"""Backend S3/MinIO (opcional): se prueba contra moto; se omite si moto/boto3 no están instalados."""
import io

import pytest

pytest.importorskip("moto")
from moto import mock_aws  # noqa: E402

from app.storage.backends import ObjectMissing, S3Backend  # noqa: E402
from config.settings import settings  # noqa: E402


def test_s3_backend_roundtrip(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(settings, "s3_endpoint_url", "")  # un .env con MinIO no debe interferir
    with mock_aws():
        b = S3Backend()
        b.ensure()
        b.put("p/b/a.txt", io.BytesIO(b"hola"), "text/plain")
        assert b"".join(b.open("p/b/a.txt").iter_chunks(2)) == b"hola"
        b.delete_prefix("p/")
        with pytest.raises(ObjectMissing):
            b.open("p/b/a.txt")
