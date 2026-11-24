"""Endpoints de autenticación: signup, login, refresh, logout, user y OAuth."""
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import schemas
from app.auth.service import OAUTH_PROVIDERS, AuthService
from app.core import security
from app.core.database import get_db
from app.core.exceptions import BadRequestError, UnauthorizedError
from app.core.tenancy import RequestContext, get_request_context, load_project, require_authenticated

router = APIRouter(prefix="/auth/v1", tags=["auth"])


def _service(db: AsyncSession, ctx: RequestContext) -> AuthService:
    return AuthService(db, ctx.project)


@router.post("/signup", response_model=schemas.SessionOut, status_code=201)
async def signup(body: schemas.SignUpRequest, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)):
    return await _service(db, ctx).sign_up(body.email, body.password, body.data)


@router.post("/login", response_model=schemas.SessionOut)
async def login(body: schemas.LoginRequest, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)):
    return await _service(db, ctx).sign_in(body.email, body.password)


@router.post("/refresh", response_model=schemas.SessionOut)
async def refresh(body: schemas.RefreshRequest, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)):
    return await _service(db, ctx).refresh(body.refresh_token)


@router.post("/logout", status_code=204)
async def logout(body: schemas.LogoutRequest, ctx: RequestContext = Depends(require_authenticated), db: AsyncSession = Depends(get_db)):
    await _service(db, ctx).sign_out(body.refresh_token, ctx.user_id)


@router.get("/user", response_model=schemas.UserOut)
async def me(ctx: RequestContext = Depends(require_authenticated), db: AsyncSession = Depends(get_db)):
    if not ctx.user_id:
        raise BadRequestError("Esta ruta requiere el token de un usuario (no una clave de servicio)")
    return await _service(db, ctx).get_user(ctx.user_id)


@router.put("/user", response_model=schemas.UserOut)
async def update_me(body: schemas.UpdateUserRequest, ctx: RequestContext = Depends(require_authenticated), db: AsyncSession = Depends(get_db)):
    if not ctx.user_id:
        raise BadRequestError("Esta ruta requiere el token de un usuario (no una clave de servicio)")
    return await _service(db, ctx).update_user(ctx.user_id, body.email, body.password, body.data)


@router.get("/authorize")
async def authorize(provider: str, redirect_to: str | None = None, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)):
    if provider not in OAUTH_PROVIDERS:
        raise BadRequestError(f"Proveedor no soportado: {provider}")
    return RedirectResponse(_service(db, ctx).authorize_url(provider, redirect_to))


@router.get("/callback", include_in_schema=False)
async def callback(code: str = Query(...), state: str = Query(...), db: AsyncSession = Depends(get_db)):
    ref = security.peek_claims(state).get("ref")
    if not isinstance(ref, str):
        raise UnauthorizedError("Estado OAuth inválido")
    project = await load_project(ref)
    claims = security.decode_token(state, project.jwt_secret)
    session = await AuthService(db, project).oauth_callback(claims["provider"], code)
    redirect_to = claims.get("redirect_to")
    if not redirect_to:
        return JSONResponse(session.model_dump(mode="json"))
    fragment = urlencode(
        {"access_token": session.access_token, "refresh_token": session.refresh_token,
         "token_type": "bearer", "expires_in": session.expires_in}
    )
    return RedirectResponse(f"{redirect_to}#{fragment}")
