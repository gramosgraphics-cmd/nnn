"""WebSocket de realtime y administración de tablas observadas.

Protocolo (JSON) cliente -> servidor:
  {"event":"subscribe","channel":"db:todos"}           cambios de la tabla (o "db:*")
  {"event":"subscribe","channel":"sala-1"}             canal de broadcast/presencia
  {"event":"unsubscribe","channel":"sala-1"}
  {"event":"broadcast","channel":"sala-1","payload":{"event":"cursor","payload":{...}}}
  {"event":"presence_track","channel":"sala-1","payload":{"nombre":"Ana"}}
  {"event":"heartbeat"}
Servidor -> cliente: postgres_changes, broadcast, presence_state/join/update/leave, subscribed, error.
"""
import logging

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.core.database import get_db, quote_ident
from app.core.exceptions import AppError, UnauthorizedError
from app.core.tenancy import RequestContext, authenticate, require_service_role
from app.database_api import service
from app.realtime.manager import Connection, manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/realtime/v1", tags=["realtime"])
TRIGGER = "baas_realtime"


class BroadcastBody(BaseModel):
    channel: str
    event: str
    payload: dict = {}


@router.websocket("/websocket")
async def websocket_endpoint(ws: WebSocket, apikey: str | None = Query(default=None), token: str | None = Query(default=None)):
    try:
        ctx = await authenticate(apikey, token)
    except AppError as exc:
        await ws.close(code=4401, reason=exc.message[:120])
        return
    await ws.accept()
    conn = Connection(ws=ws, ctx=ctx)
    manager.connect(conn)
    await conn.send({"event": "connected", "payload": {"id": conn.id, "role": ctx.role}})
    try:
        while True:
            msg = await ws.receive_json()
            await _handle(conn, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("Conexión realtime cerrada por error", exc_info=True)
    finally:
        await manager.disconnect(conn)


async def _handle(conn: Connection, msg: dict) -> None:
    event, channel = msg.get("event"), msg.get("channel")
    schema = conn.ctx.project.schema_name
    if event == "heartbeat":
        await conn.send({"event": "heartbeat", "status": "ok"})
    elif event in ("subscribe", "unsubscribe", "broadcast", "presence_track") and not isinstance(channel, str):
        await conn.send({"event": "error", "payload": {"message": "Falta 'channel'"}})
    elif event == "subscribe":
        if await manager.subscribe(conn, channel):
            await conn.send({"event": "subscribed", "channel": channel})
        else:
            await conn.send({"event": "error", "channel": channel, "payload": {"message": "Demasiados canales"}})
    elif event == "unsubscribe":
        await manager.unsubscribe(conn, channel)
    elif event == "broadcast":
        if channel.startswith("db:"):
            await conn.send({"event": "error", "channel": channel, "payload": {"message": "Canal reservado"}})
            return
        inner = msg.get("payload") or {}
        await manager.broadcast(schema, channel, str(inner.get("event", "message")), inner.get("payload"), exclude=conn)
    elif event == "presence_track":
        await manager.track_presence(conn, channel, msg.get("payload") or {})
    else:
        await conn.send({"event": "error", "payload": {"message": f"Evento desconocido: {event}"}})


@router.post("/broadcast")
async def http_broadcast(body: BroadcastBody, ctx: RequestContext = Depends(require_service_role)):
    """Envía un broadcast desde el servidor (solo service_role)."""
    if body.channel.startswith("db:"):
        raise UnauthorizedError("Canal reservado")
    sent = await manager.broadcast(ctx.project.schema_name, body.channel, body.event, body.payload)
    return {"delivered": sent}


@router.post("/enable/{table}")
async def enable_table(table: str, ctx: RequestContext = Depends(require_service_role), db: AsyncSession = Depends(get_db)):
    schema = ctx.project.schema_name
    info = await service.get_table(db, schema, table)
    target = f"{quote_ident(schema)}.{quote_ident(info.name)}"
    await db.execute(text(f"DROP TRIGGER IF EXISTS {TRIGGER} ON {target}"))
    await db.execute(text(f"CREATE TRIGGER {TRIGGER} AFTER INSERT OR UPDATE OR DELETE ON {target} FOR EACH ROW EXECUTE FUNCTION platform.notify_change()"))
    return {"table": info.name, "realtime": True}


@router.delete("/enable/{table}")
async def disable_table(table: str, ctx: RequestContext = Depends(require_service_role), db: AsyncSession = Depends(get_db)):
    schema = ctx.project.schema_name
    info = await service.get_table(db, schema, table)
    await db.execute(text(f"DROP TRIGGER IF EXISTS {TRIGGER} ON {quote_ident(schema)}.{quote_ident(info.name)}"))
    return {"table": info.name, "realtime": False}


@router.get("/stats")
async def stats(ctx: RequestContext = Depends(require_service_role)):
    return {"connections": manager.connection_count(ctx.project.schema_name)}
