"""Respaldos automáticos con pg_dump (un archivo por proyecto + metadatos de plataforma).

Se puede ejecutar como proceso independiente:  python -m app.workers.backup_worker
Requiere el binario `pg_dump` (cliente de PostgreSQL) en el PATH.
"""
import asyncio
import logging
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from app.core.database import dispose, get_engine, session_scope
from config.logging import setup_logging
from config.settings import settings

logger = logging.getLogger(__name__)
LOCK_ID = 727_001_001  # clave del advisory lock de PostgreSQL (sustituye al bloqueo en Redis)


def _pg_dump(schema: str, out_file: Path) -> None:
    url = settings.sync_dsn_parts
    exe = shutil.which("pg_dump")
    if exe is None:
        raise RuntimeError("No se encontró 'pg_dump' en el PATH (instala el cliente de PostgreSQL)")
    env = {**os.environ, "PGPASSWORD": url.password or ""}
    cmd = [exe, "-h", url.host or "localhost", "-p", str(url.port or 5432), "-U", url.username or "postgres",
           "-d", url.database or "postgres", "--schema", schema, "--format=custom", "--no-owner", "-f", str(out_file)]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:1000] or "pg_dump falló")


async def backup_schema(label: str, schema: str) -> dict:
    """Respalda un schema y registra el resultado en platform.backups."""
    backup_id = uuid.uuid4()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = Path(settings.backup_dir) / label
    directory.mkdir(parents=True, exist_ok=True)
    out_file = directory / f"{stamp}_{schema}.dump"
    async with session_scope() as s:
        await s.execute(text("INSERT INTO platform.backups (id, project_ref, file_path, status) VALUES (:i,:r,:f,'running')"), {"i": backup_id, "r": label, "f": str(out_file)})
    try:
        await asyncio.to_thread(_pg_dump, schema, out_file)
        status, error, size = "completed", None, out_file.stat().st_size
    except Exception as exc:
        logger.error("Backup fallido para %s: %s", label, exc)
        status, error, size = "failed", str(exc)[:1000], None
        out_file.unlink(missing_ok=True)
    async with session_scope() as s:
        await s.execute(
            text("UPDATE platform.backups SET status=:st, error=:e, size_bytes=:sz, finished_at=now() WHERE id=:i"),
            {"st": status, "e": error, "sz": size, "i": backup_id},
        )
    return {"id": str(backup_id), "project_ref": label, "status": status, "error": error, "size_bytes": size, "file_path": str(out_file)}


async def cleanup_old_backups() -> int:
    async with session_scope() as s:
        rows = (await s.execute(
            text("DELETE FROM platform.backups WHERE started_at < now() - make_interval(days => :d) RETURNING file_path"),
            {"d": settings.backup_retention_days},
        )).all()
    for r in rows:
        if r.file_path:
            Path(r.file_path).unlink(missing_ok=True)
    return len(rows)


async def _run_cycle() -> list[dict]:
    async with session_scope() as s:
        projects = (await s.execute(text("SELECT ref, schema_name FROM platform.projects WHERE status='active'"))).all()
    results = [await backup_schema("_platform", "platform")]
    for p in projects:
        results.append(await backup_schema(p.ref, p.schema_name))
    removed = await cleanup_old_backups()
    logger.info("Ciclo de backups terminado", extra={"backups": len(results), "eliminados": removed})
    return results


async def run_backup_cycle() -> list[dict]:
    """Un solo ciclo a la vez (aunque haya varios procesos): advisory lock de PostgreSQL, sin Redis."""
    async with get_engine().connect() as lock_conn:
        got = (await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_ID})).scalar()
        if not got:
            logger.info("Otro proceso está respaldando; se omite este ciclo")
            return []
        try:
            return await _run_cycle()
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_ID})
            await lock_conn.rollback()


async def run_forever(stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    interval = max(settings.backup_interval_hours, 1) * 3600
    logger.info("Worker de backups iniciado (cada %s h)", settings.backup_interval_hours)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            try:
                await run_backup_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error en el ciclo de backups")


async def _main() -> None:
    setup_logging(settings.log_level, settings.log_json)
    try:
        await run_forever()
    finally:
        await dispose()


if __name__ == "__main__":
    asyncio.run(_main())
