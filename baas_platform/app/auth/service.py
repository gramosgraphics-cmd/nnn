"""Lógica de autenticación: usuarios, sesiones, refresh tokens y OAuth."""
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import SessionOut, UserOut
from app.core import security
from app.core.database import quote_ident
from app.core.exceptions import BadRequestError, ConflictError, NotFoundError, UnauthorizedError
from app.core.tenancy import ProjectInfo
from config.settings import settings

logger = logging.getLogger(__name__)

OAUTH_PROVIDERS = {
    "github": {
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "userinfo_url": "https://api.github.com/user",
        "scope": "read:user user:email",
    },
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile",
    },
}


def _provider_credentials(provider: str) -> tuple[str, str]:
    cid = getattr(settings, f"oauth_{provider}_client_id", "")
    secret = getattr(settings, f"oauth_{provider}_client_secret", "")
    if provider not in OAUTH_PROVIDERS or not cid or not secret:
        raise BadRequestError(f"El proveedor OAuth '{provider}' no está configurado", code="oauth_not_configured")
    return cid, secret


def _user_out(row) -> UserOut:
    return UserOut(
        id=row.id, email=row.email, provider=row.provider,
        user_metadata=row.raw_user_meta_data or {}, app_metadata=row.app_metadata or {},
        created_at=row.created_at, last_sign_in_at=row.last_sign_in_at,
    )


class AuthService:
    def __init__(self, session: AsyncSession, project: ProjectInfo):
        self.s = session
        self.project = project
        q = quote_ident(project.schema_name)
        self.users = f"{q}.auth_users"
        self.tokens = f"{q}.auth_refresh_tokens"

    # ------------------------------------------------------------ usuarios
    async def sign_up(self, email: str, password: str, metadata: dict) -> SessionOut:
        email = email.lower()
        try:
            async with self.s.begin_nested():
                row = (
                    await self.s.execute(
                        text(
                            f"INSERT INTO {self.users} (email, password_hash, raw_user_meta_data, email_confirmed_at, last_sign_in_at)"
                            " VALUES (:email, :ph, CAST(:meta AS jsonb), now(), now()) RETURNING *"
                        ),
                        {"email": email, "ph": security.hash_password(password), "meta": _json(metadata)},
                    )
                ).one()
        except IntegrityError as exc:
            raise ConflictError("Ya existe un usuario con ese email", code="user_exists") from exc
        return await self._issue_session(row)

    async def sign_in(self, email: str, password: str) -> SessionOut:
        row = (
            await self.s.execute(text(f"SELECT * FROM {self.users} WHERE email = :e"), {"e": email.lower()})
        ).first()
        if row is None or not security.verify_password(password, row.password_hash):
            raise UnauthorizedError("Credenciales inválidas", code="invalid_credentials")
        await self.s.execute(text(f"UPDATE {self.users} SET last_sign_in_at = now() WHERE id = :id"), {"id": row.id})
        return await self._issue_session(row)

    async def get_user(self, user_id: str) -> UserOut:
        row = (await self.s.execute(text(f"SELECT * FROM {self.users} WHERE id = CAST(:id AS uuid)"), {"id": user_id})).first()
        if row is None:
            raise NotFoundError("Usuario no encontrado")
        return _user_out(row)

    async def update_user(self, user_id: str, email: str | None, password: str | None, data: dict | None) -> UserOut:
        sets, params = ["updated_at = now()"], {"id": user_id}
        if email:
            sets.append("email = :email")
            params["email"] = email.lower()
        if password:
            sets.append("password_hash = :ph")
            params["ph"] = security.hash_password(password)
        if data is not None:
            sets.append("raw_user_meta_data = CAST(:meta AS jsonb)")
            params["meta"] = _json(data)
        try:
            async with self.s.begin_nested():
                row = (
                    await self.s.execute(
                        text(f"UPDATE {self.users} SET {', '.join(sets)} WHERE id = CAST(:id AS uuid) RETURNING *"), params
                    )
                ).first()
        except IntegrityError as exc:
            raise ConflictError("Ese email ya está en uso", code="user_exists") from exc
        if row is None:
            raise NotFoundError("Usuario no encontrado")
        return _user_out(row)

    # ------------------------------------------------------------ sesiones
    async def _issue_session(self, user_row) -> SessionOut:
        user = _user_out(user_row)
        access, expires_in = security.create_user_access_token(
            ref=self.project.ref, secret=self.project.jwt_secret, user_id=str(user.id), email=user.email,
            app_metadata=user.app_metadata, user_metadata=user.user_metadata,
        )
        raw, hashed = security.new_refresh_token()
        await self.s.execute(
            text(f"INSERT INTO {self.tokens} (token_hash, user_id, expires_at) VALUES (:h, :u, :exp)"),
            {"h": hashed, "u": user.id, "exp": datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)},
        )
        return SessionOut(access_token=access, expires_in=expires_in, refresh_token=raw, user=user)

    async def refresh(self, refresh_token: str) -> SessionOut:
        """Rotación: el refresh token usado queda revocado y se emite uno nuevo."""
        row = (
            await self.s.execute(
                text(
                    f"UPDATE {self.tokens} SET revoked = true WHERE token_hash = :h AND NOT revoked AND expires_at > now()"
                    " RETURNING user_id"
                ),
                {"h": security.hash_token(refresh_token)},
            )
        ).first()
        if row is None:
            raise UnauthorizedError("Refresh token inválido o expirado", code="invalid_refresh_token")
        user = (await self.s.execute(text(f"SELECT * FROM {self.users} WHERE id = :id"), {"id": row.user_id})).one()
        return await self._issue_session(user)

    async def sign_out(self, refresh_token: str | None, user_id: str | None) -> None:
        if refresh_token:
            await self.s.execute(
                text(f"UPDATE {self.tokens} SET revoked = true WHERE token_hash = :h"), {"h": security.hash_token(refresh_token)}
            )
        elif user_id:
            await self.s.execute(
                text(f"UPDATE {self.tokens} SET revoked = true WHERE user_id = CAST(:u AS uuid)"), {"u": user_id}
            )

    # ------------------------------------------------------------ OAuth
    def authorize_url(self, provider: str, redirect_to: str | None) -> str:
        client_id, _ = _provider_credentials(provider)
        if redirect_to and not any(redirect_to.startswith(p) for p in settings.allowed_redirect_list):
            raise BadRequestError("redirect_to no está permitido (OAUTH_ALLOWED_REDIRECTS)", code="redirect_not_allowed")
        state = security.encode_token(
            {"ref": self.project.ref, "provider": provider, "redirect_to": redirect_to or ""},
            self.project.jwt_secret, timedelta(minutes=10),
        )
        cfg = OAUTH_PROVIDERS[provider]
        query = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": f"{settings.public_url}/auth/v1/callback",
                "response_type": "code",
                "scope": cfg["scope"],
                "state": state,
            }
        )
        return f"{cfg['authorize_url']}?{query}"

    async def oauth_callback(self, provider: str, code: str) -> SessionOut:
        client_id, client_secret = _provider_credentials(provider)
        cfg = OAUTH_PROVIDERS[provider]
        async with httpx.AsyncClient(timeout=10) as http:
            tok = await http.post(
                cfg["token_url"],
                headers={"Accept": "application/json"},
                data={
                    "client_id": client_id, "client_secret": client_secret, "code": code,
                    "redirect_uri": f"{settings.public_url}/auth/v1/callback", "grant_type": "authorization_code",
                },
            )
            access = tok.json().get("access_token") if tok.status_code == 200 else None
            if not access:
                raise UnauthorizedError("No se pudo completar el login con el proveedor", code="oauth_failed")
            headers = {"Authorization": f"Bearer {access}", "Accept": "application/json"}
            info = (await http.get(cfg["userinfo_url"], headers=headers)).json()
            email = info.get("email")
            if provider == "github" and not email:
                emails = (await http.get("https://api.github.com/user/emails", headers=headers)).json()
                primary = next((e for e in emails if e.get("primary") and e.get("verified")), None)
                email = primary["email"] if primary else None
        if not email:
            raise UnauthorizedError("El proveedor no devolvió un email verificado", code="oauth_no_email")
        provider_id = str(info.get("id") or info.get("sub") or "")
        meta = {"name": info.get("name"), "avatar_url": info.get("avatar_url") or info.get("picture")}
        row = (
            await self.s.execute(
                text(
                    f"INSERT INTO {self.users} (email, provider, provider_id, raw_user_meta_data, app_metadata, email_confirmed_at, last_sign_in_at)"
                    " VALUES (:email, :prov, :pid, CAST(:meta AS jsonb), CAST(:app AS jsonb), now(), now())"
                    f" ON CONFLICT (email) DO UPDATE SET last_sign_in_at = now(), provider_id = COALESCE(auth_users.provider_id, EXCLUDED.provider_id)"
                    " RETURNING *"
                ),
                {"email": email.lower(), "prov": provider, "pid": provider_id, "meta": _json(meta), "app": _json({"provider": provider})},
            )
        ).one()
        return await self._issue_session(row)


def _json(data: dict) -> str:
    import json

    return json.dumps(data, default=str)
