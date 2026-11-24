"""Ejecución aislada de funciones: subproceso Python con límites de recursos y timeout.

AVISO: es aislamiento de proceso (cwd temporal, entorno mínimo, rlimits, timeout), no un
sandbox de seguridad. Solo quien tiene la clave service_role puede desplegar código;
trata las funciones como código de confianza o ejecútalas en workers dedicados.
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass

import httpx
from sqlalchemy import text

from app.core.database import session_scope
from config.settings import settings

logger = logging.getLogger(__name__)
MARKER = "__BAAS_RESULT__"
MAX_OUTPUT = 200_000
CPU_SECONDS_LIMIT = 30
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024

HARNESS = r'''
import importlib.util, json, os, sys, traceback
MARKER = "__BAAS_RESULT__"
try:
    import resource
    for name, value in (("RLIMIT_CPU", int(os.environ.get("BAAS_CPU", "30"))),
                        ("RLIMIT_AS", int(os.environ.get("BAAS_MEM", str(512 * 1024 * 1024)))),
                        ("RLIMIT_NOFILE", 64)):
        try:
            resource.setrlimit(getattr(resource, name), (value, value))
        except Exception:
            pass
except ImportError:
    pass

def main():
    event = json.loads(sys.stdin.read() or "{}")
    spec = importlib.util.spec_from_file_location("user_function", "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.handler(event)
    sys.stdout.write("\n" + MARKER + json.dumps(result, default=str) + "\n")

try:
    main()
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    sys.exit(1)
'''


@dataclass
class ExecutionResult:
    status: str  # success | error | timeout
    duration_ms: int
    result: object = None
    logs: str = ""


def _run_python(code: str, event: dict, env_vars: dict[str, str], timeout: int) -> ExecutionResult:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="baas_fn_") as tmp:
        with open(os.path.join(tmp, "main.py"), "w", encoding="utf-8") as f:
            f.write(code)
        with open(os.path.join(tmp, "harness.py"), "w", encoding="utf-8") as f:
            f.write(HARNESS)
        env = {"PATH": os.environ.get("PATH", ""), "BAAS_CPU": str(CPU_SECONDS_LIMIT), "BAAS_MEM": str(MEMORY_LIMIT_BYTES), "PYTHONIOENCODING": "utf-8"}
        if os.environ.get("SYSTEMROOT"):  # Windows necesita esta variable para arrancar Python
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        env.update(env_vars)
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "harness.py"], input=json.dumps(event, default=str), capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=tmp, env=env,
            )
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            return ExecutionResult("timeout", int((time.monotonic() - started) * 1000), None, (out + f"\nTiempo máximo excedido ({timeout}s)")[:MAX_OUTPUT])
    duration = int((time.monotonic() - started) * 1000)
    stdout, sep, tail = proc.stdout.rpartition(MARKER)
    logs = (stdout + proc.stderr)[:MAX_OUTPUT]
    if proc.returncode != 0 or not sep:
        return ExecutionResult("error", duration, None, (proc.stdout + proc.stderr)[:MAX_OUTPUT])
    try:
        return ExecutionResult("success", duration, json.loads(tail.strip()), logs)
    except json.JSONDecodeError:
        return ExecutionResult("error", duration, None, logs + "\nLa función devolvió algo que no es JSON")


async def _run_webhook(url: str, event: dict, timeout: int) -> ExecutionResult:
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            resp = await http.post(url, json=event)
        try:
            body = resp.json()
        except ValueError:
            body = resp.text[:MAX_OUTPUT]
        status = "success" if resp.status_code < 400 else "error"
        return ExecutionResult(status, int((time.monotonic() - started) * 1000), {"status": resp.status_code, "body": body}, "")
    except httpx.HTTPError as exc:
        return ExecutionResult("error", int((time.monotonic() - started) * 1000), None, f"Error HTTP: {exc}")


async def execute(function, event: dict, project) -> ExecutionResult:
    """Ejecuta una función ya cargada (fila de platform.functions)."""
    if function.kind == "webhook":
        return await _run_webhook(function.url, event, function.timeout_seconds)
    env_vars = {str(k): str(v) for k, v in (function.env or {}).items()}
    env_vars.update({"BAAS_PROJECT_REF": project.ref, "BAAS_URL": settings.public_url})
    return await asyncio.to_thread(_run_python, function.code, event, env_vars, function.timeout_seconds)


async def record_log(function, project, event: dict, result: ExecutionResult) -> str:
    invocation_id = uuid.uuid4().hex
    async with session_scope() as session:
        await session.execute(
            text(
                "INSERT INTO platform.function_logs (function_id, project_id, status, duration_ms, request, response, logs)"
                " VALUES (:f, :p, :s, :d, CAST(:rq AS jsonb), CAST(:rs AS jsonb), :l)"
            ),
            {
                "f": function.id, "p": project.id, "s": result.status, "d": result.duration_ms,
                "rq": json.dumps(event, default=str)[:100_000], "rs": json.dumps(result.result, default=str)[:100_000], "l": result.logs,
            },
        )
    return invocation_id


_background: set[asyncio.Task] = set()


def spawn(coro) -> None:
    """Lanza una tarea en segundo plano manteniendo una referencia (evita que el GC la cancele)."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
