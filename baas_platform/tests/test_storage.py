import pytest

from app.core.exceptions import BadRequestError
from app.storage.backends import LocalBackend, ObjectMissing, get_backend
from app.storage.service import delete_project_objects, normalize_path
from tests.conftest import hdr, signup



def _file(content=b"hola mundo", name="a.txt", mime="text/plain"):
    return {"file": (name, content, mime)}


async def test_bucket_lifecycle_and_validation(client, project):
    h = hdr(project["service"])
    assert (await client.post("/storage/v1/bucket", json={"name": "docs"}, headers=h)).status_code == 201
    assert (await client.post("/storage/v1/bucket", json={"name": "docs"}, headers=h)).status_code == 409
    assert (await client.post("/storage/v1/bucket", json={"name": "public"}, headers=h)).status_code == 422  # reservado
    assert (await client.post("/storage/v1/bucket", json={"name": "Mal Nombre"}, headers=h)).status_code == 422
    assert (await client.post("/storage/v1/bucket", json={"name": "x"}, headers=hdr(project["anon"]))).status_code in (403, 422)
    assert "docs" in [b["name"] for b in (await client.get("/storage/v1/bucket", headers=h)).json()]


async def test_owner_only_access_in_private_bucket(client, project):
    await client.post("/storage/v1/bucket", json={"name": "private"}, headers=hdr(project["service"]))
    a = await signup(client, project, "st-a@example.com")
    b = await signup(client, project, "st-b@example.com")
    ha, hb = hdr(project["anon"], a["access_token"]), hdr(project["anon"], b["access_token"])

    up = await client.post("/storage/v1/object/private/carpeta/nota.txt", files=_file(), headers=ha)
    assert up.status_code == 201 and up.json()["size"] == 10 and up.json()["owner"] == a["user"]["id"]
    assert (await client.post("/storage/v1/object/private/carpeta/nota.txt", files=_file(), headers=ha)).status_code == 409

    got = await client.get("/storage/v1/object/private/carpeta/nota.txt", headers=ha)
    assert got.status_code == 200 and got.content == b"hola mundo"
    assert (await client.get("/storage/v1/object/private/carpeta/nota.txt", headers=hb)).status_code == 404
    assert (await client.delete("/storage/v1/object/private/carpeta/nota.txt", headers=hb)).status_code == 404

    listed = await client.post("/storage/v1/object/list/private", json={"prefix": "carpeta/"}, headers=ha)
    assert [o["name"] for o in listed.json()] == ["carpeta/nota.txt"]
    assert (await client.post("/storage/v1/object/list/private", json={}, headers=hb)).json() == []

    anon_up = await client.post("/storage/v1/object/private/x.txt", files=_file(), headers=hdr(project["anon"]))
    assert anon_up.status_code == 403
    assert (await client.delete("/storage/v1/object/private/carpeta/nota.txt", headers=ha)).status_code == 204


async def test_public_bucket_and_signed_urls(client, project):
    svc = hdr(project["service"])
    await client.post("/storage/v1/bucket", json={"name": "web", "public": True}, headers=svc)
    await client.post("/storage/v1/bucket", json={"name": "secure"}, headers=svc)
    await client.post("/storage/v1/object/web/logo.txt", files=_file(b"logo"), headers=svc)
    await client.post("/storage/v1/object/secure/informe.txt", files=_file(b"confidencial"), headers=svc)

    # público: sin apikey
    pub = await client.get(f"/storage/v1/object/public/{project['ref']}/web/logo.txt")
    assert pub.status_code == 200 and pub.content == b"logo"
    # un bucket privado no se sirve por la ruta pública
    assert (await client.get(f"/storage/v1/object/public/{project['ref']}/secure/informe.txt")).status_code == 404

    signed = await client.post("/storage/v1/object/sign/secure/informe.txt", json={"expiresIn": 60}, headers=svc)
    url = signed.json()["signed_url"]
    ok = await client.get(url)  # sin apikey: la firma es la credencial
    assert ok.status_code == 200 and ok.content == b"confidencial"
    assert (await client.get(url.replace("informe.txt", "otro.txt"))).status_code in (403, 404)
    assert (await client.get(url[:-5] + "abcde")).status_code == 401  # firma manipulada

    expired = await client.post("/storage/v1/object/sign/secure/informe.txt", json={"expiresIn": 1}, headers=svc)
    import asyncio

    await asyncio.sleep(2.1)
    assert (await client.get(expired.json()["signed_url"])).status_code == 401


async def test_size_and_mime_limits(client, project):
    svc = hdr(project["service"])
    await client.post("/storage/v1/bucket", json={"name": "imgs", "file_size_limit": 10, "allowed_mime_types": ["image/*"]}, headers=svc)
    big = await client.post("/storage/v1/object/imgs/a.png", files=_file(b"x" * 11, "a.png", "image/png"), headers=svc)
    assert big.status_code == 400 and big.json()["error"]["code"] == "file_too_large"
    wrong = await client.post("/storage/v1/object/imgs/a.txt", files=_file(b"x", "a.txt", "text/plain"), headers=svc)
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "mime_not_allowed"
    assert (await client.post("/storage/v1/object/imgs/a.png", files=_file(b"x", "a.png", "image/png"), headers=svc)).status_code == 201


async def test_bucket_must_be_empty_to_delete(client, project):
    svc = hdr(project["service"])
    await client.post("/storage/v1/bucket", json={"name": "tmp"}, headers=svc)
    await client.post("/storage/v1/object/tmp/f.txt", files=_file(), headers=svc)
    assert (await client.delete("/storage/v1/bucket/tmp", headers=svc)).status_code == 409
    await client.delete("/storage/v1/object/tmp/f.txt", headers=svc)
    assert (await client.delete("/storage/v1/bucket/tmp", headers=svc)).status_code == 204


def test_path_normalization_blocks_traversal():
    assert normalize_path("a//b/./c.txt") == "a/b/c.txt"
    for bad in ("../secreto", "a/../../b", "", "/"):
        with pytest.raises(BadRequestError):
            normalize_path(bad)


async def test_local_files_live_on_disk_and_project_delete_cleans_them(client, project):
    import io
    from pathlib import Path

    svc = hdr(project["service"])
    await client.post("/storage/v1/bucket", json={"name": "disk"}, headers=svc)
    await client.post("/storage/v1/object/disk/a/b/c.txt", files=_file(b"1"), headers=svc)
    on_disk = Path(get_backend().root) / project["ref"] / "disk" / "a" / "b" / "c.txt"
    assert on_disk.read_bytes() == b"1"
    # "a/b" es una carpeta: no puede existir también un objeto con esa ruta
    clash = await client.post("/storage/v1/object/disk/a/b", files=_file(b"2"), headers=svc)
    assert clash.status_code == 400 and clash.json()["error"]["code"] == "path_conflict"
    await client.delete("/storage/v1/object/disk/a/b/c.txt", headers=svc)
    assert not on_disk.exists() and not (Path(get_backend().root) / project["ref"] / "disk" / "a").exists()  # carpetas vacías podadas

    get_backend().put(f"{project['ref']}-tmp/x/y.txt", io.BytesIO(b"z"), "text/plain")
    await delete_project_objects(f"{project['ref']}-tmp")
    with pytest.raises(ObjectMissing):
        get_backend().open(f"{project['ref']}-tmp/x/y.txt")


def test_local_backend_rejects_escape(tmp_path):
    b = LocalBackend(str(tmp_path))
    for bad in ("../fuera.txt", "a/../../fuera.txt"):
        with pytest.raises(BadRequestError):
            b.open(bad)
