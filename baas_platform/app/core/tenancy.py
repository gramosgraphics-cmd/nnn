"""Resolución de proyecto/tenant y aislamiento.

Modelo de aislamiento: un *schema* de PostgreSQL por proyecto (`proj_<ref>`) y tres
roles de base de datos por proyecto (`<schema>_anon`, `<schema>_authenticated`,
`<schema>_service_role`). Cada rol solo tiene permisos sobre el schema de su proyecto,
de modo que ni siquiera el SQL arbitrario de un proyecto puede tocar a otro.
"""
import secrets
import string
import time
from dataclasses import dataclass, field
from uuid import UUID

from fastapi import Depends, Header, Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.database import get_session_factory, quote_ident
from app.core.exceptions import ForbiddenError, NotFoundError, UnauthorizedError

ROLES = ("anon", "authenticated", "service_role")


@dataclass(frozen=True)
class ProjectInfo:
    id: UUID
    ref: str
    name: str
    schema_name: str
    jwt_secret: str
    status: str = "active"


@dataclass
class RequestContext:
    project: ProjectInfo
    role: str
    claims: dict = field(default_factory=dict)

    @property
    def user_id(self) -> str | None:
        return self.claims.get("sub") if self.role == "authenticated" else None

    @property
    def db_role(self) -> str:
        return role_name(self.project.schema_name, self.role)


def role_name(schema: str, role: str) -> str:
    assert role in ROLES
    return f"{schema}_{role}"


def service_context(project: ProjectInfo) -> RequestContext:
    """Contexto interno con rol service_role (usado por el dashboard de administración)."""
    return RequestContext(project=project, role="service_role", claims={"role": "service_role"})


# ---------------------------------------------------------------- caché de proyectos
_CACHE_TTL = 30.0
_cache: dict[str, tuple[float, ProjectInfo]] = {}

_COLUMNS = "id, ref, name, schema_name, jwt_secret, status"


def _row_to_project(row) -> ProjectInfo:
    return ProjectInfo(
        id=row.id, ref=row.ref, name=row.name, schema_name=row.schema_name,
        jwt_secret=row.jwt_secret, status=row.status,
    )


def invalidate_project(ref: str | None = None) -> None:
    if ref is None:
        _cache.clear()
    else:
        _cache.pop(ref, None)


async def load_project(ref: str) -> ProjectInfo:
    cached = _cache.get(ref)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                text(f"SELECT {_COLUMNS} FROM platform.projects WHERE ref = :ref"), {"ref": ref}
            )
        ).first()
    if row is None:
        raise UnauthorizedError("Proyecto desconocido", code="unknown_project")
    project = _row_to_project(row)
    _cache[ref] = (time.monotonic() + _CACHE_TTL, project)
    return project


async def load_project_by_schema(schema: str) -> ProjectInfo | None:
    async with get_session_factory()() as session:
        row = (
            await session.execute(
                text(f"SELECT {_COLUMNS} FROM platform.projects WHERE schema_name = :s"), {"s": schema}
            )
        ).first()
    return _row_to_project(row) if row else None


async def get_project_or_404(session: AsyncSession, ref: str) -> ProjectInfo:
    row = (
        await session.execute(
            text(f"SELECT {_COLUMNS} FROM platform.projects WHERE ref = :ref"), {"ref": ref}
        )
    ).first()
    if row is None:
        raise NotFoundError("Proyecto no encontrado")
    return _row_to_project(row)


# ---------------------------------------------------------------- autenticación por apikey
async def authenticate(apikey: str | None, bearer: str | None = None) -> RequestContext:
    """Valida la `apikey` del proyecto y, si existe, el JWT de usuario (Authorization)."""
    if not apikey:
        raise UnauthorizedError("Falta la apikey del proyecto", code="missing_apikey")
    ref = security.peek_claims(apikey).get("ref")
    if not isinstance(ref, str):
        raise UnauthorizedError("apikey inválida", code="invalid_apikey")
    project = await load_project(ref)
    if project.status != "active":
        raise ForbiddenError("El proyecto no está activo", code="project_inactive")

    claims = security.decode_token(apikey, project.jwt_secret)
    if claims.get("role") not in security.API_KEY_ROLES:
        raise UnauthorizedError("apikey inválida", code="invalid_apikey")
    role = claims["role"]

    if bearer and bearer != apikey:
        user_claims = security.decode_token(bearer, project.jwt_secret)
        user_role = user_claims.get("role")
        if user_role not in ROLES:
            raise UnauthorizedError("Token inválido", code="invalid_token")
        if user_role == "authenticated" and not user_claims.get("sub"):
            raise UnauthorizedError("Token sin usuario", code="invalid_token")
        if user_claims.get("ref") not in (None, project.ref):
            raise UnauthorizedError("Token de otro proyecto", code="invalid_token")
        role, claims = user_role, user_claims
    return RequestContext(project=project, role=role, claims=claims)


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


async def get_request_context(
    request: Request,
    apikey: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    apikey_query: str | None = Query(default=None, alias="apikey", include_in_schema=False),
) -> RequestContext:
    ctx = await authenticate(apikey or apikey_query, _bearer(authorization))
    request.state.project_id = ctx.project.id
    return ctx


async def require_service_role(ctx: RequestContext = Depends(get_request_context)) -> RequestContext:
    if ctx.role != "service_role":
        raise ForbiddenError("Se requiere la clave service_role", code="service_role_required")
    return ctx


async def require_authenticated(ctx: RequestContext = Depends(get_request_context)) -> RequestContext:
    if ctx.role not in ("authenticated", "service_role"):
        raise ForbiddenError("Se requiere un usuario autenticado", code="auth_required")
    return ctx


# ---------------------------------------------------------------- aprovisionamiento
def new_project_ref() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(20))


def schema_for_ref(ref: str) -> str:
    return f"proj_{ref}"


async def provision_project(session: AsyncSession, schema: str) -> None:
    """Crea schema, roles, tablas de auth y privilegios por defecto para un proyecto."""
    q = quote_ident(schema)
    r_anon, r_auth, r_srv = (quote_ident(role_name(schema, r)) for r in ROLES)

    stmts = [
        f"CREATE ROLE {r_anon} NOLOGIN",
        f"CREATE ROLE {r_auth} NOLOGIN",
        f"CREATE ROLE {r_srv} NOLOGIN BYPASSRLS",
        f"GRANT {r_anon}, {r_auth}, {r_srv} TO CURRENT_USER",
        f"CREATE SCHEMA {q}",
        f"""CREATE TABLE {q}.auth_users (
              id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              email text UNIQUE NOT NULL,
              password_hash text,
              provider text NOT NULL DEFAULT 'email',
              provider_id text,
              raw_user_meta_data jsonb NOT NULL DEFAULT '{{}}'::jsonb,
              app_metadata jsonb NOT NULL DEFAULT '{{"provider":"email"}}'::jsonb,
              email_confirmed_at timestamptz,
              last_sign_in_at timestamptz,
              created_at timestamptz NOT NULL DEFAULT now(),
              updated_at timestamptz NOT NULL DEFAULT now()
           )""",
        f"""CREATE TABLE {q}.auth_refresh_tokens (
              id bigserial PRIMARY KEY,
              token_hash text UNIQUE NOT NULL,
              user_id uuid NOT NULL REFERENCES {q}.auth_users(id) ON DELETE CASCADE,
              revoked boolean NOT NULL DEFAULT false,
              expires_at timestamptz NOT NULL,
              created_at timestamptz NOT NULL DEFAULT now()
           )""",
        f"GRANT USAGE ON SCHEMA {q} TO {r_anon}, {r_auth}, {r_srv}",
        f"GRANT CREATE ON SCHEMA {q} TO {r_srv}",
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {r_srv} IN SCHEMA {q} GRANT ALL ON TABLES TO {r_anon}, {r_auth}",
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {r_srv} IN SCHEMA {q} GRANT ALL ON SEQUENCES TO {r_anon}, {r_auth}",
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {r_srv} IN SCHEMA {q} GRANT ALL ON FUNCTIONS TO {r_anon}, {r_auth}",
    ]
    for stmt in stmts:
        await session.execute(text(stmt))


async def deprovision_project(session: AsyncSession, schema: str) -> None:
    q = quote_ident(schema)
    roles = [quote_ident(role_name(schema, r)) for r in ROLES]
    await session.execute(text(f"DROP SCHEMA IF EXISTS {q} CASCADE"))
    await session.execute(text(f"DROP OWNED BY {', '.join(roles)}"))
    for r in roles:
        await session.execute(text(f"DROP ROLE IF EXISTS {r}"))
