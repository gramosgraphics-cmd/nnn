"""Rutas del panel (Studio): login de admin, proyectos, API keys, métricas y explorador."""
from pathlib import Path

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.database import get_db
from app.core.exceptions import UnauthorizedError
from app.core.tenancy import get_project_or_404
from app.dashboard import schemas, service
from app.database_api.schemas import SqlRequest

STATIC_DIR = Path(__file__).parent / "static"
ui_router = APIRouter(include_in_schema=False)
router = APIRouter(prefix="/dashboard/api", tags=["dashboard"])


@ui_router.get("/dashboard")
@ui_router.get("/dashboard/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


async def require_admin(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("Inicia sesión como administrador")
    return security.decode_admin_token(authorization[7:].strip())


@router.post("/login", response_model=schemas.AdminToken)
async def login(body: schemas.AdminLogin, db: AsyncSession = Depends(get_db)):
    token = await service.admin_login(db, body.email, body.password)
    return schemas.AdminToken(access_token=token, email=body.email.lower())


@router.get("/projects", response_model=list[schemas.ProjectOut])
async def projects(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await service.list_projects(db)


@router.post("/projects", response_model=schemas.ProjectOut, status_code=201)
async def create_project(body: schemas.ProjectCreate, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await service.create_project(db, body.name)


@router.delete("/projects/{ref}", status_code=204)
async def delete_project(ref: str, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    await service.delete_project(db, ref)


@router.get("/projects/{ref}/keys", response_model=schemas.ProjectKeys)
async def keys(ref: str, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await service.get_keys(db, ref)


@router.post("/projects/{ref}/keys/rotate", response_model=schemas.ProjectKeys)
async def rotate(ref: str, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await service.rotate_keys(db, ref)


@router.get("/projects/{ref}/overview", response_model=schemas.Overview)
async def overview(ref: str, range: str = Query(default="30d", pattern="^(7d|30d|12m)$"), _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await service.overview(db, await get_project_or_404(db, ref), range)


@router.get("/projects/{ref}/users")
async def users(ref: str, limit: int = Query(default=50, ge=1, le=500), offset: int = Query(default=0, ge=0), _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await service.list_users(db, await get_project_or_404(db, ref), limit, offset)


@router.get("/projects/{ref}/tables")
async def tables(ref: str, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await service.list_tables(await get_project_or_404(db, ref))


@router.get("/projects/{ref}/tables/{table}/rows")
async def rows(ref: str, table: str, limit: int = Query(default=50, ge=1, le=500), offset: int = Query(default=0, ge=0), _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await service.table_rows(await get_project_or_404(db, ref), table, limit, offset)


@router.post("/projects/{ref}/sql")
async def sql(ref: str, body: SqlRequest, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await service.run_sql(await get_project_or_404(db, ref), body.query, body.params)


@router.get("/projects/{ref}/backups")
async def backups(ref: str, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await service.list_backups(db, ref)


@router.post("/projects/{ref}/backups", status_code=201)
async def backup(ref: str, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return await service.backup_now(await get_project_or_404(db, ref))
