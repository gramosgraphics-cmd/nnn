"""Contexto de usuario/rol para Row Level Security.

Cada petición se ejecuta en una transacción donde:
  * `search_path` apunta al schema del proyecto,
  * `request.jwt.claim.*` contiene los claims (lo que leen auth.uid(), auth.role()...),
  * `SET LOCAL ROLE` cambia al rol de BD del proyecto (anon | authenticated | service_role).
"""
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session_factory, quote_ident
from app.core.tenancy import RequestContext, get_request_context
from config.settings import settings


async def apply_rls_context(session: AsyncSession, ctx: RequestContext) -> None:
    claims = {k: v for k, v in ctx.claims.items() if k not in ("iat", "exp", "iss")}
    await session.execute(
        text(
            "SELECT set_config('search_path', :sp, true),"
            " set_config('statement_timeout', :st, true),"
            " set_config('request.jwt.claims', :claims, true),"
            " set_config('request.jwt.claim.sub', :sub, true),"
            " set_config('request.jwt.claim.role', :role, true),"
            " set_config('request.jwt.claim.email', :email, true)"
        ),
        {
            "sp": quote_ident(ctx.project.schema_name),
            "st": str(settings.db_statement_timeout_ms),
            "claims": json.dumps(claims, default=str),
            "sub": str(ctx.claims.get("sub") or "") if ctx.role == "authenticated" else "",
            "role": ctx.role,
            "email": str(ctx.claims.get("email") or ""),
        },
    )
    # SET LOCAL ROLE no admite parámetros; el nombre sale de una lista cerrada y se escapa.
    await session.execute(text(f"SET LOCAL ROLE {quote_ident(ctx.db_role)}"))


@asynccontextmanager
async def rls_session(ctx: RequestContext) -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        async with session.begin():
            await apply_rls_context(session, ctx)
            yield session


async def get_rls_session(ctx: RequestContext = Depends(get_request_context)) -> AsyncIterator[AsyncSession]:
    async with rls_session(ctx) as session:
        yield session
