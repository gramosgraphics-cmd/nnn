"""Metadatos de archivos y buckets."""
import re
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

RESERVED_BUCKET_NAMES = {"public", "sign", "list"}
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")


class BucketCreate(BaseModel):
    name: str
    public: bool = False
    file_size_limit: int | None = Field(default=None, gt=0, description="Bytes")
    allowed_mime_types: list[str] | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not _BUCKET_RE.match(v) or v in RESERVED_BUCKET_NAMES:
            raise ValueError("Nombre inválido: 2-63 caracteres [a-z0-9_-] y no reservado (public, sign, list)")
        return v


class BucketOut(BaseModel):
    id: UUID
    name: str
    public: bool
    file_size_limit: int | None = None
    allowed_mime_types: list[str] | None = None
    created_at: datetime


class ObjectOut(BaseModel):
    id: UUID
    name: str
    owner: UUID | None = None
    size: int
    mime_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ListRequest(BaseModel):
    prefix: str = ""
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class SignRequest(BaseModel):
    expires_in: int = Field(default=3600, ge=1, le=7 * 24 * 3600, alias="expiresIn")
    model_config = {"populate_by_name": True}


class SignedUrlOut(BaseModel):
    signed_url: str
