from tests.conftest import hdr, signup, sql



async def test_apikey_required_and_validated(client, project):
    assert (await client.get("/rest/v1/_meta/tables")).status_code == 401
    r = await client.get("/rest/v1/_meta/tables", headers={"apikey": "no-es-un-jwt"})
    assert r.status_code == 401
    assert (await client.get("/rest/v1/_meta/tables", headers=hdr(project["anon"]))).status_code == 200


async def test_signup_login_refresh_rotation(client, project):
    anon = {"apikey": project["anon"]}
    s = await signup(client, project, "ana@example.com")
    assert s["user"]["email"] == "ana@example.com" and s["access_token"]

    dup = await client.post("/auth/v1/signup", json={"email": "ANA@example.com", "password": "password123"}, headers=anon)
    assert dup.status_code == 409

    bad = await client.post("/auth/v1/login", json={"email": "ana@example.com", "password": "mala-clave"}, headers=anon)
    assert bad.status_code == 401
    ok = await client.post("/auth/v1/login", json={"email": "ana@example.com", "password": "password123"}, headers=anon)
    assert ok.status_code == 200

    me = await client.get("/auth/v1/user", headers=hdr(project["anon"], ok.json()["access_token"]))
    assert me.status_code == 200 and me.json()["email"] == "ana@example.com"

    refreshed = await client.post("/auth/v1/refresh", json={"refresh_token": ok.json()["refresh_token"]}, headers=anon)
    assert refreshed.status_code == 200
    reuse = await client.post("/auth/v1/refresh", json={"refresh_token": ok.json()["refresh_token"]}, headers=anon)
    assert reuse.status_code == 401  # el refresh token se rota: no se puede reutilizar


async def test_short_password_rejected(client, project):
    r = await client.post("/auth/v1/signup", json={"email": "x@example.com", "password": "123"}, headers={"apikey": project["anon"]})
    assert r.status_code == 422


async def test_token_from_other_project_rejected(client, project, other_project):
    s = await signup(client, other_project, "otro@example.com")
    r = await client.get("/auth/v1/user", headers=hdr(project["anon"], s["access_token"]))
    assert r.status_code == 401


async def test_rls_isolates_rows_between_users(client, project):
    await sql(client, project, "CREATE TABLE notes (id serial PRIMARY KEY, owner uuid DEFAULT auth.uid(), body text)")
    await sql(client, project, "ALTER TABLE notes ENABLE ROW LEVEL SECURITY")
    await sql(client, project, "CREATE POLICY own_rows ON notes USING (owner = auth.uid()) WITH CHECK (owner = auth.uid())")

    a = await signup(client, project, "rls-a@example.com")
    b = await signup(client, project, "rls-b@example.com")
    ha, hb = hdr(project["anon"], a["access_token"]), hdr(project["anon"], b["access_token"])

    created = await client.post("/rest/v1/notes", json={"body": "secreto de A"}, headers=ha)
    assert created.status_code == 201, created.text
    assert created.json()[0]["owner"] == a["user"]["id"]

    assert len((await client.get("/rest/v1/notes", headers=ha)).json()) == 1
    assert (await client.get("/rest/v1/notes", headers=hb)).json() == []
    assert (await client.get("/rest/v1/notes", headers=hdr(project["anon"]))).json() == []
    assert len((await client.get("/rest/v1/notes", headers=hdr(project["service"]))).json()) == 1  # service_role salta RLS

    # B no puede modificar ni borrar filas de A
    assert (await client.patch("/rest/v1/notes?id=gt.0", json={"body": "hack"}, headers=hb)).json() == []
    assert (await client.delete("/rest/v1/notes?id=gt.0", headers=hb)).json() == []
    # B no puede insertar a nombre de A
    forged = await client.post("/rest/v1/notes", json={"body": "x", "owner": a["user"]["id"]}, headers=hb)
    assert forged.status_code == 403


async def test_tenant_isolation_at_database_level(client, project, other_project):
    await sql(client, other_project, "CREATE TABLE private_b (id int)")
    r = await client.post("/rest/v1/_sql", json={"query": f'SELECT * FROM "{other_project["schema"]}".private_b'}, headers=hdr(project["service"]))
    assert r.status_code == 403  # el rol del proyecto A no tiene acceso al schema de B
    r = await client.post("/rest/v1/_sql", json={"query": "SELECT * FROM platform.projects"}, headers=hdr(project["service"]))
    assert r.status_code == 403  # ni al plano de control
    r = await client.post("/rest/v1/_sql", json={"query": "SELECT * FROM auth_users"}, headers=hdr(project["service"]))
    assert r.status_code == 403  # tablas internas de auth ocultas


async def test_sql_runner_requires_service_role(client, project):
    r = await client.post("/rest/v1/_sql", json={"query": "SELECT 1"}, headers=hdr(project["anon"]))
    assert r.status_code == 403
