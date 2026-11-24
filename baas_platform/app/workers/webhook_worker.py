"""Despacho asíncrono de webhooks con reintentos (backoff exponencial) y firma HMAC.

Se puede ejecutar como proceso independiente:  python -m app.workers.webhook_worker
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import random
import socket
from urllib.parse import urlparse

import httpx
from sqlalchemy import text

from app.core.database import dispose, session_scope
from app.core.exceptions import BadRequestError
from config.logging import setup_logging
from config.settings import settings

logger = logging.getLogger(__name__)
BATCH_SIZE = 20
POLL_SECONDS = 2
REQUEST_TIMEOUT = 10


# ------------------------------------------------------------------ seguridad (SSRF)
async def validate_webhook_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BadRequestError("URL de webhook inválida")
    if settings.webhooks_allow_private_networks:
        return
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, parsed.port or 80, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BadRequestError("No se pudo resolver el host del webhook") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise BadRequestError("Destino en red privada no permitido (WEBHOOKS_ALLOW_PRIVATE_NETWORKS=false)", code="webhook_private_network")


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def event_matches(patterns: list[str], table: str, op: str) -> bool:
    for p in patterns:
        if p == "*":
            return True
        t, _, o = p.partition(":")
        if t in ("*", table) and o in ("", "*", op):
            return True
    return False


# ------------------------------------------------------------------ encolado
async def enqueue_deliveries(project, change: dict) -> int:
    """Crea una entrega pendiente por cada webhook del proyecto que coincida con el evento."""
    async with session_scope() as session:
        hooks = (await session.execute(text("SELECT id, events FROM platform.webhooks WHERE project_id=:p AND enabled"), {"p": project.id})).all()
        event = json.dumps({**change, "project_ref": project.ref}, default=str)
        count = 0
        for h in hooks:
            if event_matches(list(h.events), change.get("table", ""), change.get("type", "")):
                await session.execute(
                    text("INSERT INTO platform.webhook_deliveries (webhook_id, event) VALUES (:w, CAST(:e AS jsonb))"),
                    {"w": h.id, "e": event},
                )
                count += 1
    return count


# ------------------------------------------------------------------ entrega
def backoff_seconds(attempts: int) -> int:
    return min(5 * 2 ** max(attempts - 1, 0), 3600) + random.randint(0, 3)


async def _deliver(http: httpx.AsyncClient, row) -> tuple[bool, int | None, str | None]:
    body = json.dumps(row.event, default=str).encode()
    headers = {
        "Content-Type": "application/json", "X-Baas-Event-Id": str(row.id),
        "X-Baas-Delivery-Attempt": str(row.attempts + 1), "X-Baas-Signature": sign(row.secret, body),
    }
    try:
        await validate_webhook_url(row.url)
        resp = await http.post(row.url, content=body, headers=headers)
        if resp.status_code < 300:
            return True, resp.status_code, None
        return False, resp.status_code, f"HTTP {resp.status_code}"
    except Exception as exc:  # red, DNS, validación...
        return False, None, str(exc)[:500]


async def process_batch() -> int:
    """Toma entregas vencidas (FOR UPDATE SKIP LOCKED: seguro con varios workers) y las envía."""
    async with session_scope() as session:
        rows = (await session.execute(
            text(
                "SELECT d.id, d.attempts, d.event, w.url, w.secret FROM platform.webhook_deliveries d"
                " JOIN platform.webhooks w ON w.id = d.webhook_id"
                " WHERE d.status = 'pending' AND d.next_attempt_at <= now()"
                " ORDER BY d.next_attempt_at LIMIT :n FOR UPDATE OF d SKIP LOCKED"
            ),
            {"n": BATCH_SIZE},
        )).all()
        if not rows:
            return 0
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=False) as http:
            results = await asyncio.gather(*(_deliver(http, r) for r in rows))
        for row, (ok, status, error) in zip(rows, results):
            attempts = row.attempts + 1
            if ok:
                await session.execute(
                    text("UPDATE platform.webhook_deliveries SET status='delivered', attempts=:a, response_status=:s, last_error=NULL, delivered_at=now() WHERE id=:i"),
                    {"a": attempts, "s": status, "i": row.id},
                )
            elif attempts >= settings.webhook_max_attempts:
                await session.execute(
                    text("UPDATE platform.webhook_deliveries SET status='failed', attempts=:a, response_status=:s, last_error=:e WHERE id=:i"),
                    {"a": attempts, "s": status, "e": error, "i": row.id},
                )
            else:
                await session.execute(
                    text("UPDATE platform.webhook_deliveries SET attempts=:a, response_status=:s, last_error=:e, next_attempt_at = now() + make_interval(secs => :d) WHERE id=:i"),
                    {"a": attempts, "s": status, "e": error, "d": backoff_seconds(attempts), "i": row.id},
                )
    return len(rows)


async def run_forever(stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    logger.info("Worker de webhooks iniciado")
    while not stop.is_set():
        try:
            processed = await process_batch()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error en el worker de webhooks")
            processed = 0
        if processed == 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
            except asyncio.TimeoutError:
                pass


async def _main() -> None:
    setup_logging(settings.log_level, settings.log_json)
    try:
        await run_forever()
    finally:
        await dispose()


if __name__ == "__main__":
    asyncio.run(_main())
