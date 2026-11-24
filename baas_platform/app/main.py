"""Punto de entrada: API REST + WebSocket de realtime + dashboard.

Arranque:  uvicorn app.main:app --reload
"""
import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.auth.router import router as auth_router
from app.core.database import dispose, get_engine, get_session_factory
from app.core.exceptions import register_exception_handlers
from app.dashboard import service as dashboard_service
from app.dashboard.router import STATIC_DIR, router as dashboard_router, ui_router
from app.database_api.router import router as rest_router
from app.functions.router import router as functions_router, webhooks_router
from app.realtime.listener import run_listener
from app.realtime.manager import manager
from app.realtime.router import router as realtime_router
from app.storage.router import router as storage_router
from app.storage.service import ensure_storage
from app.workers import backup_worker, webhook_worker
from config.logging import setup_logging
from config.settings import settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level, settings.log_json)
    get_engine()
    async with get_session_factory()() as session:
        ready = (await session.execute(text("SELECT to_regclass('platform.config')"))).scalar()
    if ready is None:
        raise RuntimeError("La base de datos no está migrada. Ejecuta primero:  alembic upgrade head")
    if settings.is_production and settings.jwt_secret.startswith("cambia-este-secreto"):
        raise RuntimeError("Define un JWT_SECRET seguro en producción")

    await dashboard_service.ensure_admin()
    try:
        await ensure_storage()
    except Exception as exc:
        logger.warning("Storage no disponible: %s", exc)

    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(run_listener(stop), name="realtime-listener"),
        asyncio.create_task(dashboard_service.request_log.run_flusher(stop), name="request-log-flusher"),
    ]
    if settings.run_workers_in_process:
        tasks += [
            asyncio.create_task(webhook_worker.run_forever(stop), name="webhook-worker"),
            asyncio.create_task(backup_worker.run_forever(stop), name="backup-worker"),
        ]
    logger.info("baas_platform listo en %s (dashboard: /dashboard)", settings.public_url)
    try:
        yield
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await dashboard_service.request_log.flush()
        await dispose()


app = FastAPI(
    title="baas_platform",
    version="0.1.0",
    description="Alternativa abierta a Supabase: Auth + RLS, API REST dinámica, Realtime, Storage, Functions y Studio.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"], expose_headers=["Content-Range", "X-Request-ID"],
)
register_exception_handlers(app)


@app.middleware("http")
async def request_context(request: Request, call_next):
    started = time.perf_counter()
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        project_id = getattr(request.state, "project_id", None)
        if project_id is not None:
            dashboard_service.request_log.record(
                project_id, request.method, request.url.path, status, int((time.perf_counter() - started) * 1000)
            )


@app.get("/health", tags=["system"])
async def health():
    async with get_session_factory()() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ok", "realtime_connections": manager.connection_count()}


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/dashboard")


app.mount("/dashboard/static", StaticFiles(directory=STATIC_DIR), name="dashboard-static")
for r in (ui_router, dashboard_router, auth_router, rest_router, realtime_router, storage_router, functions_router, webhooks_router):
    app.include_router(r)
