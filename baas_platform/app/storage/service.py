"""Almacenamiento de objetos (carpeta local o S3/MinIO, ver backends.py) y políticas de acceso.

Las claves físicas son `<ref_proyecto>/<bucket>/<ruta>`.
Políticas: service_role todo; bucket público → lectura abierta; subir/borrar/listar
exige usuario autenticado y los no-service_role solo ven sus propios objetos.
"""
import asyncio
import logging
import mimetypes
import posixpath
from datetime import timedelta
from typing import BinaryIO

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.exceptions import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.tenancy import RequestContext
from app.storage.backends import ObjectMissing, get_backend

logger = logging.getLogger(__name__)


async def ensure_storage() -> None:
    await asyncio.to_thread(get_backend().ensure)


def normalize_path(path: str) -> str:
    clean = posixpath.normpath("/" + path.strip()).lstrip("/")
    if not clean or clean == "." or ".." in path.split("/") or len(clean) > 900:
        raise BadRequestError("Ruta de objeto inválida")
    return clean


class StorageService:
    def __init__(self, session: AsyncSession, ctx: RequestContext | None, project_id, project_ref: str):
        self.s, self.ctx, self.project_id, self.ref = session, ctx, project_id, project_ref

    def _key(self, bucket: str, path: str) -> str:
        return f"{self.ref}/{bucket}/{path}"

    # ------------------------------------------------------------ buckets
    async def create_bucket(self, name: str, public: bool, size_limit: int | None, mimes: list[str] | None):
        exists = (await self.s.execute(text("SELECT 1 FROM platform.buckets WHERE project_id=:p AND name=:n"), {"p": self.project_id, "n": name})).first()
        if exists:
            raise ConflictError("Ya existe un bucket con ese nombre", code="bucket_exists")
        row = (await self.s.execute(
            text("INSERT INTO platform.buckets (project_id, name, public, file_size_limit, allowed_mime_types) VALUES (:p,:n,:pub,:sz,:mt) RETURNING *"),
            {"p": self.project_id, "n": name, "pub": public, "sz": size_limit, "mt": mimes},
        )).one()
        return row

    async def list_buckets(self):
        return (await self.s.execute(text("SELECT * FROM platform.buckets WHERE project_id=:p ORDER BY name"), {"p": self.project_id})).all()

    async def get_bucket(self, name: str):
        row = (await self.s.execute(text("SELECT * FROM platform.buckets WHERE project_id=:p AND name=:n"), {"p": self.project_id, "n": name})).first()
        if row is None:
            raise NotFoundError("Bucket no encontrado", code="bucket_not_found")
        return row

    async def delete_bucket(self, name: str) -> None:
        bucket = await self.get_bucket(name)
        count = (await self.s.execute(text("SELECT count(*) FROM platform.objects WHERE bucket_id=:b"), {"b": bucket.id})).scalar_one()
        if count:
            raise ConflictError("El bucket no está vacío", code="bucket_not_empty")
        await self.s.execute(text("DELETE FROM platform.buckets WHERE id=:b"), {"b": bucket.id})

    # ------------------------------------------------------------ políticas
    def _is_service(self) -> bool:
        return self.ctx is not None and self.ctx.role == "service_role"

    def _require_user(self) -> None:
        if self.ctx is None or self.ctx.role == "anon":
            raise ForbiddenError("Esta operación requiere un usuario autenticado", code="auth_required")

    def _can_read(self, bucket, obj) -> bool:
        if bucket.public or self._is_service():
            return True
        return bool(self.ctx and self.ctx.user_id and obj.owner is not None and str(obj.owner) == self.ctx.user_id)

    # ------------------------------------------------------------ objetos
    async def _get_object(self, bucket, path: str):
        row = (await self.s.execute(text("SELECT * FROM platform.objects WHERE bucket_id=:b AND name=:n"), {"b": bucket.id, "n": path})).first()
        if row is None:
            raise NotFoundError("Objeto no encontrado", code="object_not_found")
        return row

    async def upload(self, bucket_name: str, path: str, fileobj: BinaryIO, content_type: str | None, upsert: bool = False):
        self._require_user()
        bucket = await self.get_bucket(bucket_name)
        path = normalize_path(path)
        fileobj.seek(0, 2)
        size = fileobj.tell()
        fileobj.seek(0)
        mime = content_type or mimetypes.guess_type(path)[0] or "application/octet-stream"
        if bucket.file_size_limit and size > bucket.file_size_limit:
            raise BadRequestError(f"El archivo supera el límite del bucket ({bucket.file_size_limit} bytes)", code="file_too_large")
        if bucket.allowed_mime_types and not any(_mime_match(p, mime) for p in bucket.allowed_mime_types):
            raise BadRequestError(f"Tipo de archivo no permitido: {mime}", code="mime_not_allowed")
        existing = (await self.s.execute(text("SELECT * FROM platform.objects WHERE bucket_id=:b AND name=:n"), {"b": bucket.id, "n": path})).first()
        if existing:
            if not upsert:
                raise ConflictError("El objeto ya existe (usa upsert=true para reemplazarlo)", code="object_exists")
            if not self._is_service() and (self.ctx.user_id is None or str(existing.owner) != self.ctx.user_id):
                raise ForbiddenError("Solo el propietario puede reemplazar este objeto")
        await asyncio.to_thread(get_backend().put, self._key(bucket_name, path), fileobj, mime)
        owner = self.ctx.user_id if self.ctx else None
        return (await self.s.execute(
            text(
                "INSERT INTO platform.objects (bucket_id, name, owner, size, mime_type) VALUES (:b,:n,CAST(:o AS uuid),:sz,:mt)"
                " ON CONFLICT (bucket_id, name) DO UPDATE SET size=EXCLUDED.size, mime_type=EXCLUDED.mime_type, updated_at=now() RETURNING *"
            ),
            {"b": bucket.id, "n": path, "o": owner, "sz": size, "mt": mime},
        )).one()

    async def open_for_download(self, bucket_name: str, path: str, *, bypass_policy: bool = False):
        bucket = await self.get_bucket(bucket_name)
        path = normalize_path(path)
        obj = await self._get_object(bucket, path)
        if not bypass_policy and not self._can_read(bucket, obj):
            raise NotFoundError("Objeto no encontrado", code="object_not_found")  # no revela su existencia
        try:
            body = await asyncio.to_thread(get_backend().open, self._key(bucket_name, path))
        except ObjectMissing as exc:
            raise NotFoundError("El archivo no está en el almacenamiento", code="object_missing") from exc
        return obj, body

    async def delete(self, bucket_name: str, path: str) -> None:
        self._require_user()
        bucket = await self.get_bucket(bucket_name)
        path = normalize_path(path)
        obj = await self._get_object(bucket, path)
        if not self._is_service() and str(obj.owner) != self.ctx.user_id:
            raise NotFoundError("Objeto no encontrado", code="object_not_found")
        await asyncio.to_thread(get_backend().delete, self._key(bucket_name, path))
        await self.s.execute(text("DELETE FROM platform.objects WHERE id=:i"), {"i": obj.id})

    async def list_objects(self, bucket_name: str, prefix: str, limit: int, offset: int):
        self._require_user()
        bucket = await self.get_bucket(bucket_name)
        sql = "SELECT * FROM platform.objects WHERE bucket_id=:b AND name LIKE :pre ESCAPE '\\'"
        params = {"b": bucket.id, "pre": prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", "lim": limit, "off": offset}
        if not self._is_service():
            sql += " AND owner = CAST(:o AS uuid)"
            params["o"] = self.ctx.user_id
        return (await self.s.execute(text(sql + " ORDER BY name LIMIT :lim OFFSET :off"), params)).all()

    # ------------------------------------------------------------ URLs firmadas
    async def sign(self, bucket_name: str, path: str, expires_in: int) -> str:
        self._require_user()
        bucket = await self.get_bucket(bucket_name)
        path = normalize_path(path)
        obj = await self._get_object(bucket, path)
        if not self._can_read(bucket, obj):
            raise NotFoundError("Objeto no encontrado", code="object_not_found")
        token = security.encode_token(
            {"ref": self.ref, "url": f"{bucket_name}/{path}"}, self.ctx.project.jwt_secret, timedelta(seconds=expires_in)
        )
        return f"/storage/v1/object/sign/{bucket_name}/{path}?token={token}"


def verify_signed_token(token: str, project) -> str:
    claims = security.decode_token(token, project.jwt_secret)
    if claims.get("ref") != project.ref or "url" not in claims:
        raise ForbiddenError("Token de firma inválido")
    return claims["url"]


def _mime_match(pattern: str, mime: str) -> bool:
    return pattern == mime or (pattern.endswith("/*") and mime.startswith(pattern[:-1]))


async def delete_project_objects(project_ref: str) -> None:
    """Borra del almacenamiento físico todo lo que pertenece a un proyecto."""

    try:
        await asyncio.to_thread(get_backend().delete_prefix, f"{project_ref}/")
    except Exception:
        logger.warning("No se pudo limpiar el almacenamiento del proyecto %s", project_ref, exc_info=True)
