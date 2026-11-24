"""Esquemas de consulta, filtros y respuestas de la API de base de datos."""
from typing import Any

from pydantic import BaseModel, Field


class ColumnInfo(BaseModel):
    name: str
    data_type: str
    nullable: bool
    default: str | None = None
    is_primary_key: bool = False


class ForeignKeyInfo(BaseModel):
    name: str
    column: str
    references_table: str
    references_column: str


class TableInfo(BaseModel):
    name: str
    kind: str = "table"  # table | view
    rls_enabled: bool = False
    columns: list[ColumnInfo]
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyInfo] = Field(default_factory=list)


class SqlRequest(BaseModel):
    query: str = Field(min_length=1, max_length=100_000, description="Una sola sentencia SQL")
    params: dict[str, Any] = Field(default_factory=dict)


class SqlResponse(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool = False


class Filter(BaseModel):
    column: str
    op: str
    value: str


class QuerySpec(BaseModel):
    select: list[str] | None = None
    filters: list[Filter] = Field(default_factory=list)
    order: list[tuple[str, bool]] = Field(default_factory=list)  # (columna, descendente)
    limit: int = 100
    offset: int = 0
