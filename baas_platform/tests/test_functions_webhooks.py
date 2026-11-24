"""Funciones aisladas y webhooks con firma, reintentos y backoff."""
import hashlib
import hmac
import http.server
import json
import threading

import pytest
from sqlalchemy import text

from app.core.database import session_scope
from app.workers import webhook_worker
from tests.conftest import hdr


async def test_python_function_success_error_and_timeout(client, project):
    h = hdr(project["service"])
    ok = "def handler(event):\n    print('log de prueba')\n    return {'doble': event['body']['n'] * 2, 'rol': event['context']['role']}\n"
    assert (await client.put("/functions/v1/doble", json={"code": ok}, headers=h)).status_code == 200
    r = (await client.post("/functions/v1/doble", json={"n": 21}, headers=h)).json()
    assert r["status"] == "success" and r["result"] == {"doble": 42, "rol": "service_role"} and "log de prueba" in r["logs"]

    await client.put("/functions/v1/falla", json={"code": "def handler(e):\n    raise ValueError('boom')\n"}, headers=h)
    r = (await client.post("/functions/v1/falla", json={}, headers=h)).json()
    assert r["status"] == "error" and "boom" in r["logs"]

    await client.put("/functions/v1/lenta", json={"code": "import time\ndef handler(e):\n    time.sleep(5)\n", "timeout_seconds": 1}, headers=h)
    assert (await client.post("/functions/v1/lenta", json={}, headers=h)).json()["status"] == "timeout"

    logs = (await client.get("/functions/v1/doble/logs", headers=h)).json()
    assert logs and logs[0]["status"] == "success"


async def test_function_access_rules_and_validation(client, project):
    h = hdr(project["service"])
    await client.put("/functions/v1/privada", json={"code": "def handler(e):\n    return 1\n"}, headers=h)
    await client.put("/functions/v1/publica", json={"code": "def handler(e):\n    return 2\n", "verify_jwt": False}, headers=h)
    anon = hdr(project["anon"])
    assert (await client.post("/functions/v1/privada", json={}, headers=anon)).status_code == 403
    assert (await client.post("/functions/v1/publica", json={}, headers=anon)).json()["result"] == 2
    assert (await client.put("/functions/v1/x", json={"code": "def handler(e): pass"}, headers=anon)).status_code == 403  # solo service_role despliega
    assert (await client.put("/functions/v1/Mal Nombre", json={"code": "x"}, headers=h)).status_code == 400
    assert (await client.put("/functions/v1/sin-codigo", json={"kind": "python"}, headers=h)).status_code == 422
    assert (await client.post("/functions/v1/no-existe", json={}, headers=h)).status_code == 404


class _Receiver:
    def __init__(self, status=200):
        outer = self
        self.status, self.calls = status, []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.calls.append((dict(self.headers), body))
                self.send_response(outer.status)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


async def _deliveries(webhook_id):
    async with session_scope() as s:
        return (await s.execute(text("SELECT * FROM platform.webhook_deliveries WHERE webhook_id=CAST(:w AS uuid) ORDER BY created_at"), {"w": webhook_id})).all()


def _change(project, op="INSERT", table="pedidos"):
    return {"schema": project["schema"], "table": table, "type": op, "record": {"id": 1}, "old_record": None}


async def test_webhook_delivery_is_signed_and_filtered(client, project):
    h = hdr(project["service"])
    recv = _Receiver()
    try:
        created = (await client.post("/webhooks/v1", json={"url": recv.url, "events": ["pedidos:INSERT"]}, headers=h)).json()
        assert created["secret"]
        assert await webhook_worker.enqueue_deliveries(project["info"], _change(project, "DELETE")) == 0  # no coincide el filtro
        assert await webhook_worker.enqueue_deliveries(project["info"], _change(project)) == 1
        await webhook_worker.process_batch()

        assert len(recv.calls) == 1
        headers, body = recv.calls[0]
        expected = "sha256=" + hmac.new(created["secret"].encode(), body, hashlib.sha256).hexdigest()
        assert headers["X-Baas-Signature"] == expected
        assert json.loads(body)["record"] == {"id": 1}
        assert [(r.status, r.attempts) for r in await _deliveries(created["id"])] == [("delivered", 1)]
        assert (await client.get("/webhooks/v1", headers=h)).json()[0].get("secret") is None  # el secreto solo se muestra al crear
        assert (await client.delete(f"/webhooks/v1/{created['id']}", headers=h)).status_code == 204
    finally:
        recv.close()


async def test_webhook_retries_with_backoff_then_fails(client, project):
    h = hdr(project["service"])
    recv = _Receiver(status=500)
    try:
        created = (await client.post("/webhooks/v1", json={"url": recv.url, "events": ["*"]}, headers=h)).json()
        await webhook_worker.enqueue_deliveries(project["info"], _change(project, table="t"))
        await webhook_worker.process_batch()
        row = (await _deliveries(created["id"]))[0]
        assert row.status == "pending" and row.attempts == 1 and row.last_error == "HTTP 500"
        assert row.next_attempt_at > row.created_at  # reprogramado con backoff

        calls_before = len(recv.calls)
        await webhook_worker.process_batch()  # aún no vence: no se reintenta
        assert len(recv.calls) == calls_before

        async with session_scope() as s:  # simulamos que ya se agotaron los intentos menos uno
            await s.execute(
                text("UPDATE platform.webhook_deliveries SET next_attempt_at = now(), attempts = :a WHERE webhook_id = CAST(:w AS uuid)"),
                {"a": webhook_worker.settings.webhook_max_attempts - 1, "w": created["id"]},
            )
        await webhook_worker.process_batch()
        assert (await _deliveries(created["id"]))[0].status == "failed"
        await client.delete(f"/webhooks/v1/{created['id']}", headers=h)
    finally:
        recv.close()


def test_backoff_grows_and_is_capped():
    values = [webhook_worker.backoff_seconds(n) for n in (1, 2, 3, 20)]
    assert values[0] < values[1] < values[2] and values[3] <= 3603


async def test_private_networks_blocked_when_configured(monkeypatch):
    from app.core.exceptions import BadRequestError

    monkeypatch.setattr(webhook_worker.settings, "webhooks_allow_private_networks", False)
    with pytest.raises(BadRequestError):
        await webhook_worker.validate_webhook_url("http://127.0.0.1:8080/x")
    with pytest.raises(BadRequestError):
        await webhook_worker.validate_webhook_url("ftp://example.com/x")
