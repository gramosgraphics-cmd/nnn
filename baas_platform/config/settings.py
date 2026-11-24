"""Variables de entorno de la plataforma (DB, JWT, S3, claves)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"
    log_json: bool = False

    # Base de datos
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/baas"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_statement_timeout_ms: int = 15000

    # Seguridad
    jwt_secret: str = "cambia-este-secreto-por-uno-largo-y-aleatorio"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 30
    platform_admin_email: str = "admin@example.com"
    platform_admin_password: str = "admin12345"

    # Storage: "local" (carpeta del teléfono/PC, por defecto) o "s3" (S3/MinIO)
    storage_backend: str = "local"
    storage_dir: str = "./storage_data"
    s3_endpoint_url: str = ""
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_region: str = "us-east-1"
    s3_bucket: str = "baas-storage"

    # Backups
    backup_dir: str = "./backups"
    backup_interval_hours: int = 24
    backup_retention_days: int = 7

    run_workers_in_process: bool = True

    # Webhooks
    webhooks_allow_private_networks: bool = False
    webhook_max_attempts: int = 6

    # OAuth
    public_url: str = "http://localhost:8000"
    oauth_allowed_redirects: str = "http://localhost:3000,http://localhost:8000"
    oauth_github_client_id: str = ""
    oauth_github_client_secret: str = ""
    oauth_google_client_id: str = ""
    oauth_google_client_secret: str = ""

    cors_origins: str = "http://localhost:3000,http://localhost:8000"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_redirect_list(self) -> list[str]:
        return [o.strip() for o in self.oauth_allowed_redirects.split(",") if o.strip()]

    @property
    def sync_dsn_parts(self):
        """Partes de conexión para herramientas libpq (pg_dump)."""
        from sqlalchemy.engine import make_url

        return make_url(self.database_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
