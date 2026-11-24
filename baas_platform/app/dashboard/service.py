"""Estadísticas, creación de proyectos y administración de la plataforma."""
import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rls import rls_session
from app.core import security
from app.core.database import session_scope
from app.core.exceptions import UnauthorizedError
from app.core.tenancy import (
    ProjectInfo, deprovision_project, get_project_or_404, invalidate_project, new_project_ref,
    provision_project, schema_for_ref, service_context,
)
from app.database_api import introspection
from app.database_api import service as db_service
from app.database_api.schemas import QuerySpec
from app.realtime.manager import manager
from app.storage.service import delete_project_objects
from app.workers import backup_worker
from config.settings import settings

logger = logging.getLogger(__name__)
RANGES = {"7d": 7, "30d": 30, "12m": 365}


# ------------------------------------------------------------------ buffer de logs de peticiones
class RequestLogBuffer:
    """Acumula peticiones en memoria y las inserta por lotes (no frena la API)."""

    def __init__(self) -> None:
        self._items: list[dict] = []

    @staticmethod
    def service_of(path: str) -> str:
        for prefix, name in (("/auth/", "auth"), ("/rest/", "rest"), ("/storage/", "storage"), ("/realtime/", "realtime"), ("/functions/", "functions"), ("/webhooks/", "functions")):
            if path.startswith(prefix):
                return name
        return "other"

    def record(self, project_id, method: str, path: str, status: int, duration_ms: int) -> None:
        if len(self._items) < 50_000:
            self._items.append({"p": project_id, "s": self.service_of(path), "m": method, "path": path[:300], "st": status, "d": duration_ms})

    async def flush(self) -> int:
        items, self._items = self._items, []
        if not items:
            return 0
        try:
            async with session_scope() as session:
                await session.execute(
                    text("INSERT INTO platform.request_logs (project_id, service, method, path, status, duration_ms) VALUES (:p,:s,:m,:path,:st,:d)"),
                    items,
                )
        except Exception:
            logger.warning("No se pudieron guardar logs de peticiones", exc_info=True)
            return 0
        return len(items)

    async def run_flusher(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            await self.flush()


request_log = RequestLogBuffer()


# ------------------------------------------------------------------ administradores
async def ensure_admin() -> None:
    async with session_scope() as s:
        exists = (await s.execute(text("SELECT 1 FROM platform.admins WHERE email=:e"), {"e": settings.platform_admin_email.lower()})).first()
        if not exists:
            await s.execute(
                text("INSERT INTO platform.admins (email, password_hash) VALUES (:e,:h)"),
                {"e": settings.platform_admin_email.lower(), "h": security.hash_password(settings.platform_admin_password)},
            )
            logger.info("Administrador inicial creado: %s", settings.platform_admin_email)


async def admin_login(session: AsyncSession, email: str, password: str) -> str:
    row = (await session.execute(text("SELECT id, email, password_hash FROM platform.admins WHERE email=:e"), {"e": email.lower()})).first()
    if row is None or not security.verify_password(password, row.password_hash):
        raise UnauthorizedError("Credenciales inválidas", code="invalid_credentials")
    return security.create_admin_token(str(row.id), row.email)


# ------------------------------------------------------------------ proyectos
async def create_project(session: AsyncSession, name: str):
    ref = new_project_ref()
    schema, secret = schema_for_ref(ref), security.new_project_secret()
    row = (await session.execute(
        text(
            "INSERT INTO platform.projects (ref, name, schema_name, jwt_secret, anon_key, service_role_key)"
            " VALUES (:r,:n,:s,:sec,:a,:sr) RETURNING id, ref, name, schema_name, status, created_at"
        ),
        {"r": ref, "n": name.strip(), "s": schema, "sec": secret,
         "a": security.create_api_key(ref, "anon", secret), "sr": security.create_api_key(ref, "service_role", secret)},
    )).one()
    await provision_project(session, schema)
    return row


async def list_projects(session: AsyncSession):
    return (await session.execute(text("SELECT id, ref, name, schema_name, status, created_at FROM platform.projects ORDER BY created_at DESC"))).all()


async def get_keys(session: AsyncSession, ref: str) -> dict:
    row = (await session.execute(text("SELECT ref, anon_key, service_role_key FROM platform.projects WHERE ref=:r"), {"r": ref})).first()
    if row is None:
        await get_project_or_404(session, ref)
    return {"ref": row.ref, "url": settings.public_url, "anon_key": row.anon_key, "service_role_key": row.service_role_key}


async def rotate_keys(session: AsyncSession, ref: str) -> dict:
    """Nuevo secreto JWT: invalida TODAS las claves y tokens de usuario emitidos."""
    await get_project_or_404(session, ref)
    secret = security.new_project_secret()
    await session.execute(
        text("UPDATE platform.projects SET jwt_secret=:s, anon_key=:a, service_role_key=:sr, updated_at=now() WHERE ref=:r"),
        {"s": secret, "a": security.create_api_key(ref, "anon", secret), "sr": security.create_api_key(ref, "service_role", secret), "r": ref},
    )
    invalidate_project(ref)
    return await get_keys(session, ref)


async def delete_project(session: AsyncSession, ref: str) -> None:
    project = await get_project_or_404(session, ref)
    await delete_project_objects(ref)
    await session.execute(text("DELETE FROM platform.request_logs WHERE project_id=:p"), {"p": project.id})
    await session.execute(text("DELETE FROM platform.projects WHERE id=:p"), {"p": project.id})
    await deprovision_project(session, project.schema_name)
    invalidate_project(ref)
    introspection.invalidate(project.schema_name)


# ------------------------------------------------------------------ explorador (como service_role del proyecto)
async def list_users(session: AsyncSession, project: ProjectInfo, limit: int, offset: int):
    from app.core.database import quote_ident

    tbl = f"{quote_ident(project.schema_name)}.auth_users"
    return (await session.execute(
        text(f"SELECT id, email, provider, created_at, last_sign_in_at FROM {tbl} ORDER BY created_at DESC LIMIT :l OFFSET :o"), {"l": limit, "o": offset}
    )).all()


async def list_tables(project: ProjectInfo):
    async with rls_session(service_context(project)) as s:
        return list((await introspection.get_schema_snapshot(s, project.schema_name, use_cache=False)).values())


async def table_rows(project: ProjectInfo, table: str, limit: int, offset: int):
    async with rls_session(service_context(project)) as s:
        info = await db_service.get_table(s, project.schema_name, table)
        rows, total = await db_service.select_rows(s, project.schema_name, info, QuerySpec(limit=min(limit, 500), offset=offset), count=True)
        return {"columns": [c.model_dump() for c in info.columns], "rows": rows, "total": total}


async def run_sql(project: ProjectInfo, query: str, params: dict):
    async with rls_session(service_context(project)) as s:
        return await db_service.run_sql(s, project.schema_name, query, params)


async def list_backups(session: AsyncSession, ref: str):
    return (await session.execute(
        text("SELECT id, project_ref, file_path, size_bytes, status, error, started_at, finished_at FROM platform.backups WHERE project_ref=:r ORDER BY started_at DESC LIMIT 50"), {"r": ref}
    )).all()


async def backup_now(project: ProjectInfo) -> dict:
    return await backup_worker.backup_schema(project.ref, project.schema_name)


# ------------------------------------------------------------------ métricas / overview
def _delta(cur: float, prev: float) -> float | None:
    if prev == 0:
        return None if cur else 0.0
    return round((cur - prev) / prev * 100, 1)


async def overview(session: AsyncSession, project: ProjectInfo, range_key: str) -> dict:
    from app.core.database import quote_ident

    days = RANGES.get(range_key, 30)
    range_key = range_key if range_key in RANGES else "30d"
    pid, users = project.id, f"{quote_ident(project.schema_name)}.auth_users"
    q = lambda sql, **p: session.execute(text(sql), {"pid": pid, "d": days, **p})  # noqa: E731

    total_users = (await q(f"SELECT count(*) FROM {users}")).scalar_one()
    new_cur = (await q(f"SELECT count(*) FROM {users} WHERE created_at >= now() - make_interval(days => :d)")).scalar_one()
    new_prev = (await q(f"SELECT count(*) FROM {users} WHERE created_at >= now() - make_interval(days => :d * 2) AND created_at < now() - make_interval(days => :d)")).scalar_one()
    active = (await q(f"SELECT count(*) FROM {users} WHERE last_sign_in_at >= now() - make_interval(days => :d)")).scalar_one()
    req = (await q(
        "SELECT count(*) FILTER (WHERE ts >= now() - make_interval(days => :d)) AS cur,"
        " count(*) FILTER (WHERE ts < now() - make_interval(days => :d)) AS prev,"
        " count(*) FILTER (WHERE ts >= now() - make_interval(days => :d) AND status >= 400) AS errors,"
        " coalesce(avg(duration_ms) FILTER (WHERE ts >= now() - make_interval(days => :d)), 0) AS avg_ms,"
        " coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) FILTER (WHERE ts >= now() - make_interval(days => :d)), 0) AS p95_ms"
        " FROM platform.request_logs WHERE project_id = :pid AND ts >= now() - make_interval(days => :d * 2)"
    )).one()

    if range_key == "12m":
        granularity = "month"
        series_sql = (
            "SELECT to_char(m, 'Mon') AS label, count(r.id) AS value FROM generate_series(date_trunc('month', now()) - interval '11 months', date_trunc('month', now()), interval '1 month') m"
            " LEFT JOIN platform.request_logs r ON r.project_id = :pid AND r.ts >= m AND r.ts < m + interval '1 month' GROUP BY m ORDER BY m"
        )
    else:
        granularity = "day"
        series_sql = (
            "SELECT to_char(g, 'DD Mon') AS label, count(r.id) AS value FROM generate_series(date_trunc('day', now()) - make_interval(days => :d - 1), date_trunc('day', now()), interval '1 day') g"
            " LEFT JOIN platform.request_logs r ON r.project_id = :pid AND r.ts >= g AND r.ts < g + interval '1 day' GROUP BY g ORDER BY g"
        )
    series = [{"label": r.label, "value": int(r.value)} for r in (await q(series_sql)).all()]

    services = [{"service": r.service, "count": int(r.count)} for r in (await q(
        "SELECT service, count(*) AS count FROM platform.request_logs WHERE project_id=:pid AND ts >= now() - make_interval(days => :d) GROUP BY service ORDER BY count DESC"
    )).all()]

    matrix = [[0] * 24 for _ in range(7)]  # lunes=0 ... domingo=6
    for r in (await q(
        "SELECT ((extract(dow FROM ts)::int + 6) % 7) AS wd, extract(hour FROM ts)::int AS hr, count(*) AS n"
        " FROM platform.request_logs WHERE project_id=:pid AND ts >= now() - interval '30 days' GROUP BY 1, 2"
    )).all():
        matrix[r.wd][r.hr] = int(r.n)
    total_hm = sum(map(sum, matrix))
    flat = [(matrix[w][h], w, h) for w in range(7) for h in range(24)]
    peak_n, peak_w, peak_h = max(flat)
    weekday_share = round(sum(sum(matrix[w]) for w in range(5)) / total_hm * 100) if total_hm else 0
    after18 = round(sum(matrix[w][h] for w in range(7) for h in range(18, 24)) / total_hm * 100) if total_hm else 0
    names = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
    heatmap = {
        "matrix": matrix, "weekdays": names, "max": peak_n,
        "peak": {"weekday": names[peak_w], "hour": peak_h, "count": peak_n} if peak_n else None,
        "weekday_share": weekday_share, "after_18_share": after18,
    }

    total_req, errors = int(req.cur), int(req.errors)
    insight = (
        f"El pico de actividad es {names[peak_w]} a las {peak_h:02d}:00; programa tareas pesadas fuera de esa franja."
        if peak_n else "Aún no hay tráfico registrado. Haz peticiones con tu apikey para ver métricas aquí."
    )
    storage = (await q(
        "SELECT count(o.id) AS objects, coalesce(sum(o.size), 0) AS bytes FROM platform.buckets b LEFT JOIN platform.objects o ON o.bucket_id = b.id WHERE b.project_id = :pid"
    )).one()
    return {
        "range": range_key, "range_days": days,
        "kpis": [
            {"key": "total_users", "label": "Usuarios totales", "value": total_users, "delta_pct": _delta(total_users, max(total_users - new_cur, 0))},
            {"key": "active_users", "label": "Usuarios activos", "value": active, "delta_pct": None},
            {"key": "requests", "label": "Peticiones API", "value": total_req, "delta_pct": _delta(total_req, int(req.prev))},
            {"key": "new_users", "label": "Usuarios nuevos", "value": new_cur, "delta_pct": _delta(new_cur, new_prev)},
        ],
        "ledger": [
            {"label": "Tasa de error", "value": f"{(errors / total_req * 100) if total_req else 0:.2f}%"},
            {"label": "Latencia media", "value": f"{float(req.avg_ms):.0f} ms"},
            {"label": "Latencia p95", "value": f"{float(req.p95_ms):.0f} ms"},
            {"label": "Peticiones / usuario", "value": f"{(total_req / total_users) if total_users else 0:.1f}"},
        ],
        "series": series, "granularity": granularity, "services": services, "heatmap": heatmap, "insight": insight,
        "realtime_connections": manager.connection_count(project.schema_name),
        "storage": {"objects": int(storage.objects), "bytes": int(storage.bytes)},
    }
