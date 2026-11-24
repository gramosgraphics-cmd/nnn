"""Almacenamiento físico de objetos: carpeta local (por defecto, ideal para Android/Termux) o S3/MinIO.

Ambos backends exponen la misma interfaz síncrona (el servicio la llama con asyncio.to_thread):
    ensure()                       prepara el almacenamiento
    put(key, fileobj, mime)        guarda un objeto
    open(key) -> body              body.iter_chunks(n) y body.close(); ObjectMissing si no existe
    delete(key)                    borra un objeto (no falla si no existe)
    delete_prefix(prefix)          borra todo lo que cuelga de un prefijo
Las claves son `<ref_proyecto>/<bucket>/<ruta>`.
"""
import errno
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, Protocol

from app.core.exceptions import BadRequestError
from config.settings import settings


class ObjectMissing(Exception):
    """El archivo no existe en el almacenamiento físico."""


class Body(Protocol):
    def iter_chunks(self, chunk_size: int = ...) -> Iterator[bytes]: ...
    def close(self) -> None: ...


# --------------------------------------------------------------------- local
class _LocalBody:
    def __init__(self, fh: BinaryIO) -> None:
        self._fh = fh

    def iter_chunks(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        try:
            while chunk := self._fh.read(chunk_size):
                yield chunk
        finally:
            self.close()

    def read(self) -> bytes:
        try:
            return self._fh.read()
        finally:
            self.close()

    def close(self) -> None:
        self._fh.close()


class LocalBackend:
    """Guarda los objetos como archivos bajo STORAGE_DIR (escritura atómica)."""

    def __init__(self, root: str) -> None:
        self.root = Path(root).expanduser().resolve()

    def _path(self, key: str) -> Path:
        target = (self.root / key).resolve()
        if self.root not in target.parents:  # defensa extra contra ../ y enlaces simbólicos
            raise BadRequestError("Ruta de objeto inválida")
        return target

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, fileobj: BinaryIO, mime: str) -> None:
        target = self._path(key)
        tmp_name = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".upload-")
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(fileobj, out, 1024 * 1024)
            os.replace(tmp_name, target)
            tmp_name = None
        except OSError as exc:
            if exc.errno in (errno.ENOTDIR, errno.EISDIR, errno.EEXIST):
                raise BadRequestError("La ruta choca con otro objeto (un objeto no puede ser a la vez archivo y carpeta)", code="path_conflict") from exc
            if exc.errno == errno.ENAMETOOLONG:
                raise BadRequestError("Algún segmento de la ruta supera los 255 caracteres") from exc
            raise
        finally:
            if tmp_name:
                Path(tmp_name).unlink(missing_ok=True)

    def open(self, key: str) -> _LocalBody:
        try:
            return _LocalBody(open(self._path(key), "rb"))
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError) as exc:
            raise ObjectMissing(key) from exc

    def _prune_empty_dirs(self, start: Path) -> None:
        for parent in start.parents:
            if parent == self.root:
                break
            try:
                parent.rmdir()  # solo borra si está vacío
            except OSError:
                break

    def delete(self, key: str) -> None:
        target = self._path(key)
        target.unlink(missing_ok=True)
        self._prune_empty_dirs(target)

    def delete_prefix(self, prefix: str) -> None:
        shutil.rmtree(self._path(prefix.rstrip("/")), ignore_errors=True)


# ------------------------------------------------------------------------ S3
class S3Backend:
    """S3 / MinIO / cualquier servicio compatible (requiere:  pip install "baas_platform[s3]")."""

    def __init__(self) -> None:
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import boto3
            from botocore.client import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint_url or None,
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
                region_name=settings.s3_region,
                config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
            )
        return self._client

    def ensure(self) -> None:
        from botocore.exceptions import ClientError

        try:
            self.client.head_bucket(Bucket=settings.s3_bucket)
        except ClientError:
            self.client.create_bucket(Bucket=settings.s3_bucket)

    def put(self, key: str, fileobj: BinaryIO, mime: str) -> None:
        self.client.upload_fileobj(fileobj, settings.s3_bucket, key, ExtraArgs={"ContentType": mime})

    def open(self, key: str):
        from botocore.exceptions import ClientError

        try:
            return self.client.get_object(Bucket=settings.s3_bucket, Key=key)["Body"]
        except ClientError as exc:
            raise ObjectMissing(key) from exc

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=settings.s3_bucket, Key=key)

    def delete_prefix(self, prefix: str) -> None:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                self.client.delete_objects(Bucket=settings.s3_bucket, Delete={"Objects": keys})


# ------------------------------------------------------------------- selector
_backend: LocalBackend | S3Backend | None = None


def get_backend() -> LocalBackend | S3Backend:
    global _backend
    if _backend is None:
        kind = settings.storage_backend.lower()
        if kind == "local":
            _backend = LocalBackend(settings.storage_dir)
        elif kind == "s3":
            _backend = S3Backend()
        else:
            raise RuntimeError(f"STORAGE_BACKEND desconocido: {settings.storage_backend!r} (usa 'local' o 's3')")
    return _backend


def reset_backend() -> None:
    global _backend
    _backend = None
