"""Lectura de tablas, columnas, tipos, claves y relaciones de un schema de proyecto."""
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database_api.schemas import ColumnInfo, ForeignKeyInfo, TableInfo

HIDDEN_PREFIX = "auth\\_"  # tablas internas de autenticación (auth_users, auth_refresh_tokens)
_TTL = 10.0
_cache: dict[str, tuple[float, dict[str, TableInfo]]] = {}

_COLUMNS_SQL = text(
    """
    SELECT c.relname AS table_name, c.relkind, c.relrowsecurity AS rls, a.attname AS column_name,
           format_type(a.atttypid, a.atttypmod) AS data_type, NOT a.attnotnull AS nullable,
           pg_get_expr(d.adbin, d.adrelid) AS column_default
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
    LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
    WHERE n.nspname = :schema AND c.relkind IN ('r', 'p', 'v')
      AND c.relname NOT LIKE :hidden ESCAPE '\\'
    ORDER BY c.relname, a.attnum
    """
)

_PK_SQL = text(
    """
    SELECT c.relname AS table_name, a.attname AS column_name
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY (i.indkey)
    WHERE i.indisprimary AND n.nspname = :schema
    """
)

_FK_SQL = text(
    """
    SELECT con.conname AS name, c.relname AS table_name, a.attname AS column_name,
           rc.relname AS ref_table, ra.attname AS ref_column
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_class rc ON rc.oid = con.confrelid
    JOIN LATERAL unnest(con.conkey, con.confkey) AS k(attnum, refnum) ON true
    JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
    JOIN pg_attribute ra ON ra.attrelid = con.confrelid AND ra.attnum = k.refnum
    WHERE con.contype = 'f' AND n.nspname = :schema
    """
)


def invalidate(schema: str | None = None) -> None:
    if schema is None:
        _cache.clear()
    else:
        _cache.pop(schema, None)


async def get_schema_snapshot(session: AsyncSession, schema: str, *, use_cache: bool = True) -> dict[str, TableInfo]:
    cached = _cache.get(schema)
    if use_cache and cached and cached[0] > time.monotonic():
        return cached[1]

    params = {"schema": schema}
    cols = (await session.execute(_COLUMNS_SQL, {**params, "hidden": f"{HIDDEN_PREFIX}%"})).all()
    pks: dict[str, list[str]] = {}
    for r in (await session.execute(_PK_SQL, params)).all():
        pks.setdefault(r.table_name, []).append(r.column_name)
    fks: dict[str, list[ForeignKeyInfo]] = {}
    for r in (await session.execute(_FK_SQL, params)).all():
        fks.setdefault(r.table_name, []).append(
            ForeignKeyInfo(name=r.name, column=r.column_name, references_table=r.ref_table, references_column=r.ref_column)
        )

    tables: dict[str, TableInfo] = {}
    for r in cols:
        t = tables.get(r.table_name)
        if t is None:
            t = tables[r.table_name] = TableInfo(
                name=r.table_name, kind="view" if r.relkind == "v" else "table", rls_enabled=bool(r.rls),
                columns=[], primary_key=pks.get(r.table_name, []), foreign_keys=fks.get(r.table_name, []),
            )
        t.columns.append(
            ColumnInfo(
                name=r.column_name, data_type=r.data_type, nullable=bool(r.nullable), default=r.column_default,
                is_primary_key=r.column_name in t.primary_key,
            )
        )
    _cache[schema] = (time.monotonic() + _TTL, tables)
    return tables
