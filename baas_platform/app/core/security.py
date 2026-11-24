"""Hashing de contraseñas, JWT y claves anon/service_role."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.exceptions import UnauthorizedError
from config.settings import settings

API_KEY_ROLES = ("anon", "service_role")
ISSUER = "baas_platform"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except ValueError:
        return False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def encode_token(claims: dict, secret: str, expires_in: timedelta | None) -> str:
    payload = {"iss": ISSUER, "iat": _now(), **claims}
    if expires_in is not None:
        payload["exp"] = _now() + expires_in
    return jwt.encode(payload, secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, secret: str) -> dict:
    try:
        return jwt.decode(token, secret, algorithms=[settings.jwt_algorithm], issuer=ISSUER)
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("El token ha expirado", code="token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("Token inválido", code="invalid_token") from exc


def peek_claims(token: str) -> dict:
    """Lee los claims SIN verificar la firma (solo para localizar el proyecto)."""
    try:
        return jwt.decode(token, options={"verify_signature": False})
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("Token inválido", code="invalid_token") from exc


def create_api_key(ref: str, role: str, secret: str) -> str:
    """Clave larga duración (10 años) tipo Supabase: role = anon | service_role."""
    assert role in API_KEY_ROLES
    return encode_token({"role": role, "ref": ref}, secret, timedelta(days=3650))


def create_user_access_token(
    *, ref: str, secret: str, user_id: str, email: str | None, app_metadata: dict, user_metadata: dict
) -> tuple[str, int]:
    expires = timedelta(minutes=settings.access_token_expire_minutes)
    token = encode_token(
        {
            "sub": user_id,
            "role": "authenticated",
            "ref": ref,
            "email": email,
            "app_metadata": app_metadata,
            "user_metadata": user_metadata,
        },
        secret,
        expires,
    )
    return token, int(expires.total_seconds())


def create_admin_token(admin_id: str, email: str) -> str:
    return encode_token(
        {"sub": admin_id, "role": "platform_admin", "email": email},
        settings.jwt_secret,
        timedelta(hours=12),
    )


def decode_admin_token(token: str) -> dict:
    claims = decode_token(token, settings.jwt_secret)
    if claims.get("role") != "platform_admin":
        raise UnauthorizedError("Token de administrador requerido")
    return claims


def new_refresh_token() -> tuple[str, str]:
    """Devuelve (token_en_claro, hash_sha256)."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_project_secret() -> str:
    return secrets.token_urlsafe(48)
