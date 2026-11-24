"""Engine de SQLAlchemy/asyncpg y sesiones."""
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from config.settings import settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def quote_ident(name: str) -> str:
    """Escapa un identificador SQL (equivalente a quote_ident de PostgreSQL)."""
    return '"' + name.replace('"', '""') + '"'


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            connect_args={"server_settings": {"timezone": "UTC", "application_name": "baas_platform"}},
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Sesión con transacción: commit al salir, rollback si hay excepción."""
    async with get_session_factory()() as session:
        async with session.begin():
            yield session


async def get_db() -> AsyncIterator[AsyncSession]:
    """Dependencia FastAPI: sesión de plataforma (sin RLS)."""
    async with session_scope() as session:
        yield session


def raw_asyncpg_dsn() -> str:
    """DSN para conexiones asyncpg directas (LISTEN/NOTIFY)."""
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def dispose() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
