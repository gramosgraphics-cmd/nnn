"""Endpoints de buckets, subida/descarga de objetos y URLs firmadas."""
from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.tenancy import RequestContext, get_request_context, load_project, require_service_role
from app.storage import schemas
from app.storage.service import StorageService, verify_signed_token

router = APIRouter(prefix="/storage/v1", tags=["storage"])


def _svc(db: AsyncSession, ctx: RequestContext) -> StorageService:
    return StorageService(db, ctx, ctx.project.id, ctx.project.ref)


def _stream(obj, body) -> StreamingResponse:
    headers = {"Content-Length": str(obj.size), "Cache-Control": "private, max-age=3600"}
    return StreamingResponse(body.iter_chunks(64 * 1024), media_type=obj.mime_type or "application/octet-stream", headers=headers)


# ---- buckets
@router.get("/bucket", response_model=list[schemas.BucketOut])
async def list_buckets(ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)):
    return await _svc(db, ctx).list_buckets()


@router.post("/bucket", response_model=schemas.BucketOut, status_code=201)
async def create_bucket(body: schemas.BucketCreate, ctx: RequestContext = Depends(require_service_role), db: AsyncSession = Depends(get_db)):
    return await _svc(db, ctx).create_bucket(body.name, body.public, body.file_size_limit, body.allowed_mime_types)


@router.get("/bucket/{name}", response_model=schemas.BucketOut)
async def get_bucket(name: str, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)):
    return await _svc(db, ctx).get_bucket(name)


@router.delete("/bucket/{name}", status_code=204)
async def delete_bucket(name: str, ctx: RequestContext = Depends(require_service_role), db: AsyncSession = Depends(get_db)):
    await _svc(db, ctx).delete_bucket(name)


# ---- acceso sin apikey: URLs públicas y firmadas (las rutas fijas van ANTES de las genéricas)
@router.get("/object/public/{ref}/{bucket}/{path:path}")
async def download_public(ref: str, bucket: str, path: str, db: AsyncSession = Depends(get_db)):
    project = await load_project(ref)
    svc = StorageService(db, None, project.id, project.ref)
    b = await svc.get_bucket(bucket)
    if not b.public:
        from app.core.exceptions import NotFoundError

        raise NotFoundError("Objeto no encontrado", code="object_not_found")
    obj, body = await svc.open_for_download(bucket, path)
    return _stream(obj, body)


@router.get("/object/sign/{bucket}/{path:path}")
async def download_signed(bucket: str, path: str, token: str = Query(...), db: AsyncSession = Depends(get_db)):
    from app.core import security

    ref = security.peek_claims(token).get("ref")
    project = await load_project(str(ref))
    if verify_signed_token(token, project) != f"{bucket}/{path}":
        from app.core.exceptions import ForbiddenError

        raise ForbiddenError("La firma no corresponde a este objeto")
    svc = StorageService(db, None, project.id, project.ref)
    obj, body = await svc.open_for_download(bucket, path, bypass_policy=True)
    return _stream(obj, body)


@router.post("/object/sign/{bucket}/{path:path}", response_model=schemas.SignedUrlOut)
async def create_signed_url(bucket: str, path: str, body: schemas.SignRequest, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)):
    return {"signed_url": await _svc(db, ctx).sign(bucket, path, body.expires_in)}


@router.post("/object/list/{bucket}", response_model=list[schemas.ObjectOut])
async def list_objects(bucket: str, body: schemas.ListRequest, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)):
    return await _svc(db, ctx).list_objects(bucket, body.prefix, body.limit, body.offset)


# ---- objetos autenticados
@router.post("/object/{bucket}/{path:path}", response_model=schemas.ObjectOut, status_code=201)
async def upload(bucket: str, path: str, file: UploadFile = File(...), upsert: bool = Form(default=False),
                 ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)):
    return await _svc(db, ctx).upload(bucket, path, file.file, file.content_type, upsert)


@router.get("/object/{bucket}/{path:path}")
async def download(bucket: str, path: str, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)):
    obj, body = await _svc(db, ctx).open_for_download(bucket, path)
    return _stream(obj, body)


@router.delete("/object/{bucket}/{path:path}", status_code=204)
async def delete_object(bucket: str, path: str, ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)):
    await _svc(db, ctx).delete(bucket, path)
    return Response(status_code=204)
