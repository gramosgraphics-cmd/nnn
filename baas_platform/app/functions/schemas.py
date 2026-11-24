"""Payloads y logs de ejecución de funciones y webhooks."""
import re
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")


class FunctionDeploy(BaseModel):
    kind: str = Field(default="python", pattern="^(python|webhook)$")
    code: str | None = Field(default=None, max_length=200_000, description="Python con `def handler(event): ...`")
    url: str | None = Field(default=None, description="Destino cuando kind=webhook")
    timeout_seconds: int = Field(default=10, ge=1, le=60)
    env: dict[str, str] = Field(default_factory=dict)
    verify_jwt: bool = True

    @model_validator(mode="after")
    def _check(self):
        if self.kind == "python" and not self.code:
            raise ValueError("kind=python requiere 'code'")
        if self.kind == "webhook" and not (self.url and self.url.startswith(("http://", "https://"))):
            raise ValueError("kind=webhook requiere 'url' http(s)")
        return self


def validate_function_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise ValueError("Nombre inválido: 2-63 caracteres [a-z0-9_-], empieza por letra")
    return name


class FunctionOut(BaseModel):
    id: UUID
    name: str
    kind: str
    url: str | None = None
    timeout_seconds: int
    verify_jwt: bool
    created_at: datetime
    updated_at: datetime


class InvokeResult(BaseModel):
    invocation_id: str | None = None
    status: str
    duration_ms: int = 0
    result: Any = None
    logs: str = ""


class FunctionLogOut(BaseModel):
    id: int
    status: str
    duration_ms: int
    request: Any = None
    response: Any = None
    logs: str | None = None
    created_at: datetime


class WebhookCreate(BaseModel):
    url: str
    events: list[str] = Field(default_factory=lambda: ["*"], description="Patrones 'tabla:OPERACION', p.ej. 'todos:INSERT', '*:DELETE', '*'")
    secret: str | None = Field(default=None, min_length=16, description="Se genera si se omite")

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("La URL debe ser http(s)")
        return v


class WebhookOut(BaseModel):
    id: UUID
    url: str
    events: list[str]
    enabled: bool
    created_at: datetime
    secret: str | None = None  # solo se devuelve al crear


class DeliveryOut(BaseModel):
    id: UUID
    status: str
    attempts: int
    next_attempt_at: datetime
    last_error: str | None = None
    response_status: int | None = None
    created_at: datetime
    delivered_at: datetime | None = None
