import pytest

from tests.conftest import hdr, sql



@pytest.fixture(scope="module", autouse=True)
async def _tables(client, project):
    await sql(client, project, "CREATE TABLE authors (id serial PRIMARY KEY, name text NOT NULL)")
    await sql(client, project, (
        "CREATE TABLE books (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), title text NOT NULL, pages int, "
        "price numeric(8,2), published boolean DEFAULT false, tags text[], meta jsonb, created timestamptz DEFAULT now(), "
        "author_id int REFERENCES authors(id))"
    ))


async def test_introspection_reports_columns_keys_and_relations(client, project):
    r = await client.get("/rest/v1/_meta/tables", headers=hdr(project["service"]))
    tables = {t["name"]: t for t in r.json()}
    assert {"authors", "books"} <= set(tables)
    assert "auth_users" not in tables
    books = tables["books"]
    assert books["primary_key"] == ["id"]
    assert books["foreign_keys"][0]["references_table"] == "authors"
    assert next(c for c in books["columns"] if c["name"] == "tags")["data_type"] == "text[]"


async def test_crud_with_filters_ordering_and_pagination(client, project):
    h = hdr(project["service"])
    author = (await client.post("/rest/v1/authors", json={"name": "Cervantes"}, headers=h)).json()[0]
    rows = [
        {"title": "Quijote", "pages": 900, "price": "19.90", "published": True, "tags": ["clasico", "novela"], "meta": {"lang": "es"}, "author_id": author["id"]},
        {"title": "Novelas ejemplares", "pages": 300, "price": 12.5, "tags": [], "author_id": author["id"]},
        {"title": "La Galatea", "pages": None},
    ]
    created = await client.post("/rest/v1/books", json=rows, headers=h)
    assert created.status_code == 201 and len(created.json()) == 3

    assert len((await client.get("/rest/v1/books", headers=h)).json()) == 3
    assert [b["title"] for b in (await client.get("/rest/v1/books?pages=gt.500&select=title", headers=h)).json()] == ["Quijote"]
    assert len((await client.get("/rest/v1/books?pages=in.(300,900)", headers=h)).json()) == 2
    assert len((await client.get("/rest/v1/books?pages=is.null", headers=h)).json()) == 1
    assert len((await client.get("/rest/v1/books?title=ilike.%25NOVELA%25", headers=h)).json()) == 1
    assert (await client.get("/rest/v1/books?published=eq.true", headers=h)).json()[0]["tags"] == ["clasico", "novela"]

    ordered = (await client.get("/rest/v1/books?pages=not.x&order=pages.desc", headers=h))
    assert ordered.status_code == 400  # operador desconocido

    page = await client.get("/rest/v1/books?order=title.asc&limit=1&offset=1", headers={**h, "Prefer": "count=exact"})
    assert page.json()[0]["title"] == "Novelas ejemplares"
    assert page.headers["content-range"] == "1-1/3"


async def test_update_and_delete_require_filters(client, project):
    h = hdr(project["service"])
    assert (await client.patch("/rest/v1/books", json={"pages": 1}, headers=h)).status_code == 400
    assert (await client.delete("/rest/v1/books", headers=h)).status_code == 400
    updated = await client.patch("/rest/v1/books?title=eq.La Galatea", json={"pages": 250, "tags": ["pastoral"]}, headers=h)
    assert updated.json()[0]["pages"] == 250 and updated.json()[0]["tags"] == ["pastoral"]
    deleted = await client.delete("/rest/v1/books?title=eq.La Galatea", headers=h)
    assert len(deleted.json()) == 1


async def test_rejects_unknown_tables_columns_and_injection(client, project):
    h = hdr(project["service"])
    assert (await client.get("/rest/v1/nope", headers=h)).status_code == 404
    assert (await client.get("/rest/v1/books?select=ghost", headers=h)).status_code == 400
    assert (await client.post("/rest/v1/books", json={"title": "x", "ghost": 1}, headers=h)).status_code == 400
    injection = await client.get('/rest/v1/books?title;DROP TABLE books;--=eq.1', headers=h)
    assert injection.status_code == 400
    assert (await client.get("/rest/v1/books?title=eq.' OR 1=1 --", headers=h)).json() == []
    assert (await client.get("/rest/v1/books", headers=h)).status_code == 200  # la tabla sigue existiendo
    assert (await client.get("/rest/v1/auth_users", headers=h)).status_code == 404


async def test_type_errors_and_constraints_return_clean_errors(client, project):
    h = hdr(project["service"])
    bad_type = await client.get("/rest/v1/books?pages=eq.abc", headers=h)
    assert bad_type.status_code == 400 and bad_type.json()["error"]["code"] == "database_error"
    missing = await client.post("/rest/v1/books", json={"pages": 3}, headers=h)  # title NOT NULL
    assert missing.status_code == 400


async def test_sql_runner_returns_rows_and_applies_ddl(client, project):
    out = await sql(client, project, "SELECT count(*) AS n FROM authors")
    assert out["columns"] == ["n"] and out["rows"][0]["n"] == 1
    await sql(client, project, "CREATE TABLE temp_t (id int)")
    tables = (await client.get("/rest/v1/_meta/tables", headers=hdr(project["service"]))).json()
    assert "temp_t" in [t["name"] for t in tables]  # el caché se invalida tras DDL
