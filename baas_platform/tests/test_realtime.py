import asyncio

from app.core.tenancy import RequestContext
from app.realtime.manager import Connection, ConnectionManager, manager
from app.workers.webhook_worker import event_matches
from tests.conftest import hdr, signup, sql



class FakeSocket:
    """Sustituye al WebSocket: guarda lo que el servidor le envía."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()

    async def send_json(self, data):
        await self.queue.put(data)

    async def next(self, timeout=5.0, event=None):
        while True:
            msg = await asyncio.wait_for(self.queue.get(), timeout)
            if event is None or msg.get("event") == event:
                return msg


def make_conn(project, role="service_role", claims=None) -> Connection:
    return Connection(ws=FakeSocket(), ctx=RequestContext(project=project["info"], role=role, claims=claims or {"role": role}))


async def test_broadcast_reaches_subscribers_but_not_sender():
    mgr = ConnectionManager()

    class P:  # contexto mínimo
        schema_name = "proj_x"

    ctx = RequestContext(project=P(), role="anon", claims={})
    a, b, c = (Connection(ws=FakeSocket(), ctx=ctx) for _ in range(3))
    for conn in (a, b, c):
        mgr.connect(conn)
    await mgr.subscribe(a, "sala")
    await mgr.subscribe(b, "sala")
    sent = await mgr.broadcast("proj_x", "sala", "ping", {"n": 1}, exclude=a)
    assert sent == 1
    assert (await b.ws.next())["payload"] == {"event": "ping", "payload": {"n": 1}}
    assert a.ws.queue.empty() and c.ws.queue.empty()
    await mgr.disconnect(b)
    assert mgr.subscriber_count("proj_x", "sala") == 1


async def test_presence_join_state_and_leave():
    mgr = ConnectionManager()

    class P:
        schema_name = "proj_p"

    ctx = RequestContext(project=P(), role="anon", claims={})
    a, b = Connection(ws=FakeSocket(), ctx=ctx), Connection(ws=FakeSocket(), ctx=ctx)
    await mgr.subscribe(a, "room")
    await mgr.track_presence(a, "room", {"name": "Ana"})
    await mgr.subscribe(b, "room")
    state = await b.ws.next(event="presence_state")
    assert state["payload"][a.id] == {"name": "Ana"}
    await mgr.unsubscribe(a, "room")
    leave = await b.ws.next(event="presence_leave")
    assert leave["payload"]["id"] == a.id


async def test_postgres_changes_are_delivered_respecting_rls(client, project):
    await sql(client, project, "CREATE TABLE todos (id serial PRIMARY KEY, owner uuid DEFAULT auth.uid(), title text)")
    await sql(client, project, "ALTER TABLE todos ENABLE ROW LEVEL SECURITY")
    await sql(client, project, "CREATE POLICY own ON todos USING (owner = auth.uid()) WITH CHECK (owner = auth.uid())")
    enabled = await client.post("/realtime/v1/enable/todos", headers=hdr(project["service"]))
    assert enabled.status_code == 200

    a = await signup(client, project, "rt-a@example.com")
    b = await signup(client, project, "rt-b@example.com")
    admin = make_conn(project)
    user_a = make_conn(project, "authenticated", {"sub": a["user"]["id"], "role": "authenticated"})
    user_b = make_conn(project, "authenticated", {"sub": b["user"]["id"], "role": "authenticated"})
    for conn in (admin, user_a, user_b):
        manager.connect(conn)
        await manager.subscribe(conn, "db:todos")

    try:
        r = await client.post("/rest/v1/todos", json={"title": "comprar pan"}, headers=hdr(project["anon"], a["access_token"]))
        assert r.status_code == 201

        admin_msg = await admin.ws.next(event="postgres_changes")
        assert admin_msg["payload"]["type"] == "INSERT" and admin_msg["payload"]["record"]["title"] == "comprar pan"
        a_msg = await user_a.ws.next(event="postgres_changes")
        assert a_msg["payload"]["record"]["title"] == "comprar pan"
        await asyncio.sleep(0.3)
        assert user_b.ws.queue.empty()  # B no puede ver esa fila (RLS) => no recibe nada

        # DELETE: solo se entrega la clave primaria, nunca el contenido
        await client.delete("/rest/v1/todos?title=eq.comprar pan", headers=hdr(project["service"]))
        deleted = await user_a.ws.next(event="postgres_changes")
        assert deleted["payload"]["type"] == "DELETE" and list(deleted["payload"]["old_record"]) == ["id"]
    finally:
        for conn in (admin, user_a, user_b):
            await manager.disconnect(conn)


async def test_http_broadcast_requires_service_role(client, project):
    assert (await client.post("/realtime/v1/broadcast", json={"channel": "x", "event": "e"}, headers=hdr(project["anon"]))).status_code == 403
    assert (await client.post("/realtime/v1/broadcast", json={"channel": "db:x", "event": "e"}, headers=hdr(project["service"]))).status_code == 401
    ok = await client.post("/realtime/v1/broadcast", json={"channel": "x", "event": "e"}, headers=hdr(project["service"]))
    assert ok.status_code == 200 and ok.json() == {"delivered": 0}


def test_webhook_event_patterns():
    assert event_matches(["*"], "todos", "INSERT")
    assert event_matches(["todos:INSERT"], "todos", "INSERT")
    assert not event_matches(["todos:INSERT"], "todos", "DELETE")
    assert event_matches(["*:DELETE"], "otra", "DELETE")
    assert event_matches(["todos"], "todos", "UPDATE")
    assert not event_matches(["x:*"], "todos", "UPDATE")
