"""Rutas CRUD dinámicas estilo PostgREST, filtros, paginación y SQL runner.

  GET    /rest/v1/_meta/tables             estructura del esquema
  POST   /rest/v1/_sql                     SQL runner (service_role)
  GET    /rest/v1/{tabla}?select=a,b&col=eq.5&order=id.desc&limit=20&offset=0
  POST   /rest/v1/{tabla}                  inserta un objeto o una lista
  PATCH  /rest/v1/{tabla}?id=eq.5          actualiza filas filtradas
  DELETE /rest/v1/{tabla}?id=eq.5          elimina filas filtradas
Todas las consultas se ejecutan con RLS (rol anon / authenticated / service_role).
"""
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.rls import get_rls_session
from app.core.tenancy import RequestContext, get_request_context, require_service_role
from app.database_api import introspection, service
from app.database_api.schemas import SqlRequest, SqlResponse, TableInfo

router = APIRouter(prefix="/rest/v1", tags=["database"])


def _spec(request: Request):
    return service.parse_query_params(dict(request.query_params), list(request.query_params.multi_items()))


@router.get("/_meta/tables", response_model=list[TableInfo])
async def list_tables(ctx: RequestContext = Depends(get_request_context), session: AsyncSession = Depends(get_rls_session)):
    # La introspección usa la sesión con el rol del proyecto: solo ve lo que ese rol puede ver.
    tables = await introspection.get_schema_snapshot(session, ctx.project.schema_name, use_cache=False)
    return list(tables.values())


@router.post("/_sql", response_model=SqlResponse)
async def sql_runner(body: SqlRequest, ctx: RequestContext = Depends(require_service_role), session: AsyncSession = Depends(get_rls_session)):
    return await service.run_sql(session, ctx.project.schema_name, body.query, body.params)


@router.get("/{table}")
async def read(
    table: str, request: Request, response: Response,
    prefer: str | None = Header(default=None),
    ctx: RequestContext = Depends(get_request_context), session: AsyncSession = Depends(get_rls_session),
):
    schema = ctx.project.schema_name
    info = await service.get_table(session, schema, table)
    spec = _spec(request)
    want_count = bool(prefer and "count=exact" in prefer)
    rows, total = await service.select_rows(session, schema, info, spec, count=want_count)
    if want_count:
        end = spec.offset + max(len(rows) - 1, 0)
        response.headers["Content-Range"] = f"{spec.offset}-{end}/{total}" if rows else f"*/{total}"
    return rows


@router.post("/{table}", status_code=201)
async def create(table: str, body: dict[str, Any] | list[dict[str, Any]] = Body(...),
                 ctx: RequestContext = Depends(get_request_context), session: AsyncSession = Depends(get_rls_session)):
    info = await service.get_table(session, ctx.project.schema_name, table)
    return await service.insert_rows(session, ctx.project.schema_name, info, body)


@router.patch("/{table}")
async def update(table: str, request: Request, body: dict[str, Any] = Body(...),
                 ctx: RequestContext = Depends(get_request_context), session: AsyncSession = Depends(get_rls_session)):
    info = await service.get_table(session, ctx.project.schema_name, table)
    return await service.update_rows(session, ctx.project.schema_name, info, body, _spec(request).filters)


@router.delete("/{table}")
async def delete(table: str, request: Request,
                 ctx: RequestContext = Depends(get_request_context), session: AsyncSession = Depends(get_rls_session)):
    info = await service.get_table(session, ctx.project.schema_name, table)
    return await service.delete_rows(session, ctx.project.schema_name, info, _spec(request).filters)
