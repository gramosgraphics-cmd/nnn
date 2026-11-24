"""Escucha de cambios en PostgreSQL con LISTEN/NOTIFY (canal secreto definido en platform.config)."""
import asyncio
import json
import logging

import asyncpg
from sqlalchemy import text

from app.core.database import get_session_factory, raw_asyncpg_dsn
from app.core.tenancy import load_project_by_schema
from app.realtime.manager import manager

logger = logging.getLogger(__name__)
_projects: dict[str, object] = {}


async def get_channel_name() -> str:
    async with get_session_factory()() as session:
        return (await session.execute(text("SELECT value FROM platform.config WHERE key = 'realtime_channel'"))).scalar_one()


async def handle_notification(payload: str) -> None:
    """Procesa un NOTIFY: entrega a websockets y encola webhooks."""
    try:
        change = json.loads(payload)
    except json.JSONDecodeError:
        logger.warning("Payload de NOTIFY inválido")
        return
    schema = change.get("schema", "")
    project = _projects.get(schema) or await load_project_by_schema(schema)
    if project is None:
        return
    _projects[schema] = project
    try:
        await manager.dispatch_db_change(project, change)
    except Exception:
        logger.exception("Error entregando cambio a websockets")
    try:
        from app.workers.webhook_worker import enqueue_deliveries

        await enqueue_deliveries(project, change)
    except Exception:
        logger.exception("Error encolando webhooks")


async def run_listener(stop: asyncio.Event | None = None) -> None:
    """Bucle con reconexión automática."""
    stop = stop or asyncio.Event()
    channel = await get_channel_name()
    while not stop.is_set():
        conn = None
        try:
            conn = await asyncpg.connect(raw_asyncpg_dsn())
            loop = asyncio.get_running_loop()

            def _on_notify(_c, _pid, _chan, payload):
                loop.create_task(handle_notification(payload))

            await conn.add_listener(channel, _on_notify)
            logger.info("Realtime escuchando cambios de PostgreSQL")
            while not stop.is_set() and not conn.is_closed():
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Listener de PostgreSQL desconectado; reintentando en 3s", exc_info=True)
            await asyncio.sleep(3)
        finally:
            if conn is not None and not conn.is_closed():
                try:
                    await conn.close(timeout=2)
                except Exception:
                    pass
