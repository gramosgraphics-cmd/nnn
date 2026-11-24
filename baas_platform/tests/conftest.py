"""Fixtures: base de datos de pruebas, cliente HTTP, proyectos y usuarios.

Requiere PostgreSQL accesible (en Termux: scripts/start.sh; en PC cualquier PostgreSQL 14+).
Usa una base `baas_test` independiente (se recrea en cada ejecución). El storage usa una
carpeta temporal local.
"""
import asyncio
import os
import shutil
import tempfile

# --- el entorno se fija ANTES de importar la aplicación ---
TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/baas_test")
_STORAGE_TMP = tempfile.mkdtemp(prefix="baas-test-storage-")
os.environ.update(
    DATABASE_URL=TEST_DB_URL, STORAGE_BACKEND="local", STORAGE_DIR=_STORAGE_TMP, RUN_WORKERS_IN_PROCESS="false",
    WEBHOOKS_ALLOW_PRIVATE_NETWORKS="true", ENVIRONMENT="test", LOG_LEVEL="WARNING",
)

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def _recreate_database() -> None:
    url = make_url(TEST_DB_URL)
    conn = await asyncpg.connect(
        host=url.host, port=url.port or 5432, user=url.username, password=url.password, database="postgres"
    )
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{url.database}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def _database():
    asyncio.run(_recreate_database())
    cfg = Config(toml_file=os.path.join(ROOT, "pyproject.toml"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(scope="session", autouse=True)
def _storage_dir():
    yield
    shutil.rmtree(_STORAGE_TMP, ignore_errors=True)


@pytest_asyncio.fixture(scope="session")
async def client(_database):
    from app.main import app

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def _make_project(name: str) -> dict:
    from app.core.database import session_scope
    from app.core.tenancy import load_project
    from app.dashboard import service

    async with session_scope() as s:
        row = await service.create_project(s, name)
        keys = await service.get_keys(s, row.ref)
    info = await load_project(row.ref)
    return {"ref": row.ref, "schema": row.schema_name, "anon": keys["anon_key"], "service": keys["service_role_key"], "info": info}


async def _drop_project(project: dict) -> None:
    from app.core.database import session_scope
    from app.dashboard import service

    async with session_scope() as s:
        await service.delete_project(s, project["ref"])


@pytest_asyncio.fixture(scope="session")
async def project(client):
    p = await _make_project("Proyecto A")
    yield p
    await _drop_project(p)


@pytest_asyncio.fixture(scope="session")
async def other_project(client):
    p = await _make_project("Proyecto B")
    yield p
    await _drop_project(p)


def hdr(apikey: str, token: str | None = None) -> dict:
    return {"apikey": apikey, "Authorization": f"Bearer {token or apikey}"}


async def signup(client, project: dict, email: str, password: str = "password123") -> dict:
    r = await client.post("/auth/v1/signup", json={"email": email, "password": password}, headers={"apikey": project["anon"]})
    assert r.status_code == 201, r.text
    return r.json()


async def sql(client, project: dict, query: str):
    r = await client.post("/rest/v1/_sql", json={"query": query}, headers=hdr(project["service"]))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def service_headers(project):
    return hdr(project["service"])
