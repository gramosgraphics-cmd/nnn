"""DTOs del dashboard."""
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class AdminLogin(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class AdminToken(BaseModel):
    access_token: str
    token_type: str = "bearer"
    email: str


class ProjectCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)


class ProjectOut(BaseModel):
    id: UUID
    ref: str
    name: str
    schema_name: str
    status: str
    created_at: datetime


class ProjectKeys(BaseModel):
    ref: str
    url: str
    anon_key: str
    service_role_key: str


class Kpi(BaseModel):
    key: str
    label: str
    value: float
    delta_pct: float | None = None


class SeriesPoint(BaseModel):
    label: str
    value: int


class Overview(BaseModel):
    range: str
    range_days: int
    kpis: list[Kpi]
    ledger: list[dict[str, Any]]
    series: list[SeriesPoint]
    granularity: str
    services: list[dict[str, Any]]
    heatmap: dict[str, Any]
    insight: str
    realtime_connections: int
    storage: dict[str, Any]
