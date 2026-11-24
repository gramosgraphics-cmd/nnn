"""Construcción segura de consultas (identificadores validados + parámetros enlazados).

Nunca se interpolan valores del cliente en el SQL: los nombres de tabla/columna se
validan contra el esquema introspectado y se escapan; los valores viajan como parámetros
y se convierten con CAST al tipo real de la columna.
"""
import json
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import quote_ident
from app.core.exceptions import AppError, BadRequestError, ForbiddenError, NotFoundError
from app.database_api import introspection
from app.database_api.schemas import Filter, QuerySpec, SqlResponse, TableInfo

MAX_LIMIT = 1000
OPERATORS = {
    "eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
    "like": "LIKE", "ilike": "ILIKE",
}
RESERVED_PARAMS = {"select", "order", "limit", "offset", "apikey"}
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DatabaseError(AppError):
    status_code = 400
    code = "database_error"


def translate_db_error(exc: DBAPIError) -> AppError:
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(getattr(orig, "__cause__", None), "sqlstate", None)
    message = str(orig).split("\n")[0] if orig else "Error de base de datos"
    message = re.sub(r"^<class '[^']+'>:\s*", "", message)
    if sqlstate == "42501":
        return ForbiddenError("Permiso denegado (revisa tus políticas RLS y privilegios)", code="permission_denied")
    if sqlstate == "23505":
        return AppError(message, code="unique_violation")
    if sqlstate == "57014":
        return AppError("La consulta superó el tiempo máximo", code="statement_timeout")
    return DatabaseError(message, details={"sqlstate": sqlstate})


# ---------------------------------------------------------------- conversión de valores
def _array_literal(values: list) -> str:
    def esc(v: Any) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, (list, tuple)):
            return _array_literal(list(v))
        s = str(v).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{s}"'

    return "{" + ",".join(esc(v) for v in values) + "}"


def to_text_param(value: Any, data_type: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list) and data_type.endswith("[]"):
        return _array_literal(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _cast(param: str, data_type: str) -> str:
    # Doble CAST: el parámetro viaja como texto y PostgreSQL lo convierte al tipo real.
    return f"CAST(CAST(:{param} AS text) AS {data_type})"


# ---------------------------------------------------------------- validación
async def get_table(session: AsyncSession, schema: str, table: str) -> TableInfo:
    tables = await introspection.get_schema_snapshot(session, schema)
    info = tables.get(table)
    if info is None:
        raise NotFoundError(f"La tabla '{table}' no existe", code="table_not_found")
    return info


def _column_types(info: TableInfo) -> dict[str, str]:
    return {c.name: c.data_type for c in info.columns}


def parse_query_params(params: dict[str, str], multi: list[tuple[str, str]]) -> QuerySpec:
    spec = QuerySpec()
    if "select" in params and params["select"].strip() not in ("", "*"):
        spec.select = [c.strip() for c in params["select"].split(",") if c.strip()]
    if "order" in params:
        for part in params["order"].split(","):
            col, _, direction = part.strip().partition(".")
            if col:
                spec.order.append((col, direction.lower() == "desc"))
    try:
        spec.limit = min(int(params.get("limit", 100)), MAX_LIMIT)
        spec.offset = max(int(params.get("offset", 0)), 0)
    except ValueError as exc:
        raise BadRequestError("limit y offset deben ser enteros") from exc
    if spec.limit < 0:
        raise BadRequestError("limit no puede ser negativo")
    for key, raw in multi:
        if key in RESERVED_PARAMS:
            continue
        op, _, value = raw.partition(".")
        if op not in OPERATORS and op not in ("in", "is"):
            raise BadRequestError(f"Operador de filtro no soportado: '{op}' (columna {key})")
        spec.filters.append(Filter(column=key, op=op, value=value))
    return spec


def _where(info: TableInfo, filters: list[Filter]) -> tuple[str, dict[str, Any]]:
    types, clauses, params = _column_types(info), [], {}
    for i, f in enumerate(filters):
        if f.column not in types:
            raise BadRequestError(f"La columna '{f.column}' no existe en '{info.name}'", code="column_not_found")
        col, dtype = quote_ident(f.column), types[f.column]
        if f.op == "is":
            val = f.value.lower()
            if val not in ("null", "true", "false"):
                raise BadRequestError("is solo admite null, true o false")
            clauses.append(f"{col} IS {val.upper()}")
        elif f.op == "in":
            raw = f.value.strip("()")
            items = [v.strip().strip('"') for v in raw.split(",") if v.strip()]
            if not items:
                raise BadRequestError("in requiere al menos un valor")
            names = []
            for j, item in enumerate(items):
                name = f"f{i}_{j}"
                params[name] = item
                names.append(_cast(name, dtype))
            clauses.append(f"{col} IN ({', '.join(names)})")
        else:
            name = f"f{i}"
            params[name] = f.value
            right = f":{name}" if f.op in ("like", "ilike") else _cast(name, dtype)
            left = f"{col}::text" if f.op in ("like", "ilike") else col
            clauses.append(f"{left} {OPERATORS[f.op]} {right}")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _qualified(schema: str, table: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(table)}"


# ---------------------------------------------------------------- CRUD
async def select_rows(session: AsyncSession, schema: str, info: TableInfo, spec: QuerySpec, *, count: bool = False):
    types = _column_types(info)
    cols = "*"
    if spec.select:
        for c in spec.select:
            if c not in types:
                raise BadRequestError(f"La columna '{c}' no existe en '{info.name}'", code="column_not_found")
        cols = ", ".join(quote_ident(c) for c in spec.select)
    where, params = _where(info, spec.filters)
    order = ""
    if spec.order:
        parts = []
        for col, desc in spec.order:
            if col not in types:
                raise BadRequestError(f"No se puede ordenar por '{col}': no existe", code="column_not_found")
            parts.append(f"{quote_ident(col)} {'DESC' if desc else 'ASC'}")
        order = " ORDER BY " + ", ".join(parts)
    table = _qualified(schema, info.name)
    sql = f"SELECT {cols} FROM {table}{where}{order} LIMIT {int(spec.limit)} OFFSET {int(spec.offset)}"
    try:
        rows = [dict(r) for r in (await session.execute(text(sql), params)).mappings().all()]
        total = None
        if count:
            total = (await session.execute(text(f"SELECT count(*) FROM {table}{where}"), params)).scalar_one()
    except DBAPIError as exc:
        raise translate_db_error(exc) from exc
    return rows, total


async def insert_rows(session: AsyncSession, schema: str, info: TableInfo, payload: dict | list[dict]) -> list[dict]:
    items = payload if isinstance(payload, list) else [payload]
    if not items:
        raise BadRequestError("No hay filas que insertar")
    types, out = _column_types(info), []
    for n, item in enumerate(items):
        if not isinstance(item, dict):
            raise BadRequestError("Cada fila debe ser un objeto JSON")
        unknown = set(item) - set(types)
        if unknown:
            raise BadRequestError(f"Columnas desconocidas: {', '.join(sorted(unknown))}", code="column_not_found")
        table = _qualified(schema, info.name)
        if item:
            cols = list(item)
            params = {f"v{i}": to_text_param(item[c], types[c]) for i, c in enumerate(cols)}
            values = ", ".join(_cast(f"v{i}", types[c]) for i, c in enumerate(cols))
            sql = f"INSERT INTO {table} ({', '.join(quote_ident(c) for c in cols)}) VALUES ({values}) RETURNING *"
        else:
            params, sql = {}, f"INSERT INTO {table} DEFAULT VALUES RETURNING *"
        try:
            async with session.begin_nested():
                out.extend(dict(r) for r in (await session.execute(text(sql), params)).mappings().all())
        except DBAPIError as exc:
            raise translate_db_error(exc) from exc
    return out


async def update_rows(session: AsyncSession, schema: str, info: TableInfo, values: dict, filters: list[Filter]) -> list[dict]:
    if not filters:
        raise BadRequestError("PATCH requiere al menos un filtro (evita actualizar toda la tabla)", code="filter_required")
    types = _column_types(info)
    unknown = set(values) - set(types)
    if not values or unknown:
        raise BadRequestError(f"Columnas inválidas: {', '.join(sorted(unknown)) or '(cuerpo vacío)'}", code="column_not_found")
    params = {f"u{i}": to_text_param(values[c], types[c]) for i, c in enumerate(values)}
    sets = ", ".join(f"{quote_ident(c)} = {_cast(f'u{i}', types[c])}" for i, c in enumerate(values))
    where, wparams = _where(info, filters)
    sql = f"UPDATE {_qualified(schema, info.name)} SET {sets}{where} RETURNING *"
    try:
        return [dict(r) for r in (await session.execute(text(sql), {**params, **wparams})).mappings().all()]
    except DBAPIError as exc:
        raise translate_db_error(exc) from exc


async def delete_rows(session: AsyncSession, schema: str, info: TableInfo, filters: list[Filter]) -> list[dict]:
    if not filters:
        raise BadRequestError("DELETE requiere al menos un filtro (evita vaciar la tabla)", code="filter_required")
    where, params = _where(info, filters)
    sql = f"DELETE FROM {_qualified(schema, info.name)}{where} RETURNING *"
    try:
        return [dict(r) for r in (await session.execute(text(sql), params)).mappings().all()]
    except DBAPIError as exc:
        raise translate_db_error(exc) from exc


# ---------------------------------------------------------------- SQL runner
async def run_sql(session: AsyncSession, schema: str, query: str, params: dict[str, Any], *, max_rows: int = 1000) -> SqlResponse:
    """Ejecuta UNA sentencia con el rol del contexto (service_role del proyecto)."""
    query = query.strip().rstrip(";")
    if not query:
        raise BadRequestError("La consulta está vacía")
    try:
        async with session.begin_nested():
            result = await session.execute(text(query), params)
            if result.returns_rows:
                all_rows = result.mappings().fetchmany(max_rows + 1)
                truncated = len(all_rows) > max_rows
                rows = [dict(r) for r in all_rows[:max_rows]]
                response = SqlResponse(columns=list(result.keys()), rows=rows, row_count=len(rows), truncated=truncated)
            else:
                response = SqlResponse(columns=[], rows=[], row_count=max(result.rowcount, 0))
    except DBAPIError as exc:
        raise translate_db_error(exc) from exc
    introspection.invalidate(schema)  # el esquema pudo cambiar (DDL)
    return response
