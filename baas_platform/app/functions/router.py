"""Invocación de funciones y gestión de funciones/webhooks."""
import secrets
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import BadRequestError, ForbiddenError, NotFoundError
from app.core.tenancy import RequestContext, get_request_context, require_service_role
from app.functions import runner, schemas
from app.workers.webhook_worker import validate_webhook_url

router = APIRouter(prefix="/functions/v1", tags=["functions"])
webhooks_router = APIRouter(prefix="/webhooks/v1", tags=["webhooks"])


async def _get_function(db: AsyncSession, ctx: RequestContext, name: str):
    row = (await db.execute(text("SELECT * FROM platform.functions WHERE project_id=:p AND name=:n"), {"p": ctx.project.id, "n": name})).first()
    if row is None:
        raise NotFoundError("Función no encontrada", code="function_not_found")
    return row


@router.get("", response_model=list[schemas.FunctionOut])
async def list_functions(ctx: RequestContext = Depends(require_service_role), db: AsyncSession = Depends(get_db)):
    return (await db.execute(text("SELECT * FROM platform.functions WHERE project_id=:p ORDER BY name"), {"p": ctx.project.id})).all()


@router.put("/{name}", response_model=schemas.FunctionOut)
async def deploy(name: str, body: schemas.FunctionDeploy, ctx: RequestContext = Depends(require_service_role), db: AsyncSession = Depends(get_db)):
    try:
        schemas.validate_function_name(name)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    if body.kind == "webhook":
        await validate_webhook_url(body.url)
    import json

    return (await db.execute(
        text(
            "INSERT INTO platform.functions (project_id, name, kind, code, url, timeout_seconds, env, verify_jwt)"
            " VALUES (:p,:n,:k,:c,:u,:t,CAST(:e AS jsonb),:v)"
            " ON CONFLICT (project_id, name) DO UPDATE SET kind=EXCLUDED.kind, code=EXCLUDED.code, url=EXCLUDED.url,"
            " timeout_seconds=EXCLUDED.timeout_seconds, env=EXCLUDED.env, verify_jwt=EXCLUDED.verify_jwt, updated_at=now() RETURNING *"
        ),
        {"p": ctx.project.id, "n": name, "k": body.kind, "c": body.code, "u": body.url, "t": body.timeout_seconds, "e": json.dumps(body.env), "v": body.verify_jwt},
    )).one()


@router.delete("/{name}", status_code=204)
async def remove(name: str, ctx: RequestContext = Depends(require_service_role), db: AsyncSession = Depends(get_db)):
    fn = await _get_function(db, ctx, name)
    await db.execute(text("DELETE FROM platform.functions WHERE id=:i"), {"i": fn.id})


@router.get("/{name}/logs", response_model=list[schemas.FunctionLogOut])
async def logs(name: str, limit: int = Query(default=50, ge=1, le=500), ctx: RequestContext = Depends(require_service_role), db: AsyncSession = Depends(get_db)):
    fn = await _get_function(db, ctx, name)
    return (await db.execute(text("SELECT * FROM platform.function_logs WHERE function_id=:f ORDER BY id DESC LIMIT :l"), {"f": fn.id, "l": limit})).all()


@router.post("/{name}", response_model=schemas.InvokeResult)
async def invoke(
    name: str, request: Request, body: Any = Body(default=None),
    run_async: bool = Query(default=False, alias="async"),
    ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db),
):
    fn = await _get_function(db, ctx, name)
    if fn.verify_jwt and ctx.role == "anon":
        raise ForbiddenError("Esta función requiere un usuario autenticado (verify_jwt)", code="auth_required")
    event = {
        "method": request.method, "body": body,
        "query": {k: v for k, v in request.query_params.items() if k not in ("async", "apikey")},
        "context": {"role": ctx.role, "user_id": ctx.user_id, "project_ref": ctx.project.ref},
    }
    if run_async:
        async def _job() -> None:
            await runner.record_log(fn, ctx.project, event, await runner.execute(fn, event, ctx.project))

        runner.spawn(_job())
        return schemas.InvokeResult(status="accepted")
    result = await runner.execute(fn, event, ctx.project)
    invocation_id = await runner.record_log(fn, ctx.project, event, result)
    return schemas.InvokeResult(invocation_id=invocation_id, status=result.status, duration_ms=result.duration_ms, result=result.result, logs=result.logs)


# ------------------------------------------------------------------ webhooks
@webhooks_router.get("", response_model=list[schemas.WebhookOut])
async def list_webhooks(ctx: RequestContext = Depends(require_service_role), db: AsyncSession = Depends(get_db)):
    return (await db.execute(text("SELECT id, url, events, enabled, created_at FROM platform.webhooks WHERE project_id=:p ORDER BY created_at"), {"p": ctx.project.id})).all()


@webhooks_router.post("", response_model=schemas.WebhookOut, status_code=201)
async def create_webhook(body: schemas.WebhookCreate, ctx: RequestContext = Depends(require_service_role), db: AsyncSession = Depends(get_db)):
    await validate_webhook_url(body.url)
    secret = body.secret or secrets.token_urlsafe(32)
    row = (await db.execute(
        text("INSERT INTO platform.webhooks (project_id, url, events, secret) VALUES (:p,:u,:e,:s) RETURNING id, url, events, enabled, created_at"),
        {"p": ctx.project.id, "u": body.url, "e": body.events, "s": secret},
    )).one()
    return schemas.WebhookOut(**row._mapping, secret=secret)


@webhooks_router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(webhook_id: str, ctx: RequestContext = Depends(require_service_role), db: AsyncSession = Depends(get_db)):
    res = await db.execute(text("DELETE FROM platform.webhooks WHERE id=CAST(:i AS uuid) AND project_id=:p"), {"i": webhook_id, "p": ctx.project.id})
    if res.rowcount == 0:
        raise NotFoundError("Webhook no encontrado")


@webhooks_router.get("/{webhook_id}/deliveries", response_model=list[schemas.DeliveryOut])
async def deliveries(webhook_id: str, limit: int = Query(default=50, ge=1, le=500), ctx: RequestContext = Depends(require_service_role), db: AsyncSession = Depends(get_db)):
    return (await db.execute(
        text(
            "SELECT d.* FROM platform.webhook_deliveries d JOIN platform.webhooks w ON w.id=d.webhook_id"
            " WHERE w.id=CAST(:i AS uuid) AND w.project_id=:p ORDER BY d.created_at DESC LIMIT :l"
        ),
        {"i": webhook_id, "p": ctx.project.id, "l": limit},
    )).all()
