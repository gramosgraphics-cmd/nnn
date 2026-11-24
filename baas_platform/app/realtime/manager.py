"""Gestión de conexiones activas por proyecto/canal: suscripciones, broadcast y presencia."""
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import text

from app.auth.rls import rls_session
from app.core.database import get_session_factory, quote_ident
from app.core.tenancy import ProjectInfo, RequestContext
from app.database_api import introspection

logger = logging.getLogger(__name__)
MAX_CHANNELS_PER_CONNECTION = 100


class SocketLike(Protocol):
    async def send_json(self, data: Any) -> None: ...


@dataclass(eq=False)
class Connection:
    ws: SocketLike
    ctx: RequestContext
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    channels: set[str] = field(default_factory=set)

    async def send(self, message: dict) -> bool:
        try:
            await self.ws.send_json(message)
            return True
        except Exception:
            return False


class ConnectionManager:
    def __init__(self) -> None:
        self._subs: dict[tuple[str, str], set[Connection]] = {}
        self._presence: dict[tuple[str, str], dict[str, dict]] = {}
        self._conns: dict[str, set[Connection]] = {}

    # ------------------------------------------------------------ ciclo de vida
    def connect(self, conn: Connection) -> None:
        self._conns.setdefault(conn.ctx.project.schema_name, set()).add(conn)

    async def disconnect(self, conn: Connection) -> None:
        for channel in list(conn.channels):
            await self.unsubscribe(conn, channel)
        schema = conn.ctx.project.schema_name
        self._conns.get(schema, set()).discard(conn)

    def connection_count(self, schema: str | None = None) -> int:
        if schema:
            return len(self._conns.get(schema, ()))
        return sum(len(v) for v in self._conns.values())

    def subscriber_count(self, schema: str, channel: str) -> int:
        return len(self._subs.get((schema, channel), ()))

    # ------------------------------------------------------------ suscripciones
    async def subscribe(self, conn: Connection, channel: str) -> bool:
        if channel not in conn.channels and len(conn.channels) >= MAX_CHANNELS_PER_CONNECTION:
            return False
        key = (conn.ctx.project.schema_name, channel)
        self._subs.setdefault(key, set()).add(conn)
        conn.channels.add(channel)
        if key in self._presence:
            await conn.send({"event": "presence_state", "channel": channel, "payload": self._presence[key]})
        return True

    async def unsubscribe(self, conn: Connection, channel: str) -> None:
        key = (conn.ctx.project.schema_name, channel)
        self._subs.get(key, set()).discard(conn)
        if not self._subs.get(key):
            self._subs.pop(key, None)
        conn.channels.discard(channel)
        presence = self._presence.get(key)
        if presence and presence.pop(conn.id, None) is not None:
            await self._fanout(key, {"event": "presence_leave", "channel": channel, "payload": {"id": conn.id}})
            if not presence:
                self._presence.pop(key, None)

    # ------------------------------------------------------------ mensajes
    async def _fanout(self, key: tuple[str, str], message: dict, *, exclude: Connection | None = None) -> int:
        sent = 0
        for conn in list(self._subs.get(key, ())):
            if conn is exclude:
                continue
            if await conn.send(message):
                sent += 1
            else:
                await self.disconnect(conn)
        return sent

    async def broadcast(self, schema: str, channel: str, event: str, payload: Any, *, exclude: Connection | None = None) -> int:
        message = {"event": "broadcast", "channel": channel, "payload": {"event": event, "payload": payload}}
        return await self._fanout((schema, channel), message, exclude=exclude)

    async def track_presence(self, conn: Connection, channel: str, payload: dict) -> None:
        key = (conn.ctx.project.schema_name, channel)
        if conn not in self._subs.get(key, ()):
            return
        state = self._presence.setdefault(key, {})
        joined = conn.id not in state
        state[conn.id] = payload
        await self._fanout(key, {"event": "presence_join" if joined else "presence_update", "channel": channel, "payload": {"id": conn.id, "state": payload}})

    # ------------------------------------------------------------ cambios de base de datos
    async def dispatch_db_change(self, project: ProjectInfo, change: dict) -> int:
        """Entrega un cambio de PostgreSQL a quienes escuchan `db:<tabla>` (o `db:*`) respetando RLS."""
        schema, table = project.schema_name, change.get("table", "")
        sent = 0
        for channel in (f"db:{table}", "db:*"):
            for conn in list(self._subs.get((schema, channel), ())):
                message = await self._authorize_change(conn, project, change)
                if message is not None and await conn.send({"event": "postgres_changes", "channel": channel, "payload": message}):
                    sent += 1
        return sent

    async def _authorize_change(self, conn: Connection, project: ProjectInfo, change: dict) -> dict | None:
        if conn.ctx.role == "service_role":
            return change
        table, op = change["table"], change["type"]
        async with get_session_factory()() as catalog_session:
            snapshot = await introspection.get_schema_snapshot(catalog_session, project.schema_name)
        info = snapshot.get(table)
        if info is None or not info.primary_key or change.get("truncated"):
            return None  # sin PK no se puede comprobar visibilidad: solo service_role
        record = change.get("old_record") if op == "DELETE" else change.get("record")
        if not record or any(pk not in record for pk in info.primary_key):
            return None
        if op == "DELETE":
            # La fila ya no existe: solo se entrega su clave, nunca el contenido.
            return {**change, "record": None, "old_record": {pk: record[pk] for pk in info.primary_key}}
        conds = " AND ".join(f"{quote_ident(pk)} = CAST(CAST(:p{i} AS text) AS {next(c.data_type for c in info.columns if c.name == pk)})" for i, pk in enumerate(info.primary_key))
        params = {f"p{i}": str(record[pk]) for i, pk in enumerate(info.primary_key)}
        try:
            async with rls_session(conn.ctx) as session:
                visible = (await session.execute(text(f"SELECT 1 FROM {quote_ident(project.schema_name)}.{quote_ident(table)} WHERE {conds}"), params)).first()
        except Exception:
            logger.debug("Comprobación RLS de realtime fallida", exc_info=True)
            return None
        return {**change, "old_record": None} if visible else None



manager = ConnectionManager()
