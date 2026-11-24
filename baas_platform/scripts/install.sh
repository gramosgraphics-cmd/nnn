#!/usr/bin/env bash
# Instalación completa en Android (Termux): paquetes, PostgreSQL, entorno Python, .env y migraciones.
# Es seguro volver a ejecutarlo: no pisa lo que ya existe.
#   bash scripts/install.sh
# Variables útiles:  SKIP_PKG=1 (no usar pkg; para probar fuera de Termux)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
cd "$BAAS_DIR"

# ---------------------------------------------------------------- 1. paquetes
if [ "${SKIP_PKG:-0}" != "1" ]; then
  is_termux || die "Esto se ejecuta dentro de Termux (instálalo desde F-Droid o GitHub, no desde Google Play)."
  say "Instalando paquetes (python, postgresql, compiladores)..."
  pkg update -y
  # clang/make/rust/libffi/openssl: pydantic-core, bcrypt y asyncpg se compilan en el teléfono
  pkg install -y python postgresql clang make rust binutils libffi openssl curl
fi
command -v python3 >/dev/null || die "Falta python3"
command -v initdb  >/dev/null || die "Falta PostgreSQL (initdb)"

# ---------------------------------------------------------------- 2. entorno Python
if is_termux; then
  # Las extensiones en Rust (pydantic-core, bcrypt) necesitan saber la versión de Android.
  export ANDROID_API_LEVEL="${ANDROID_API_LEVEL:-$(getprop ro.build.version.sdk 2>/dev/null || echo 24)}"
  export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}"   # menos paralelismo = menos RAM al compilar
fi
if [ ! -d "$VENV" ]; then
  say "Creando entorno virtual..."
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
say "Instalando dependencias de Python (la primera vez puede tardar 10-30 min compilando)..."
python -m pip install --upgrade pip wheel setuptools
python -m pip install -e .

# ---------------------------------------------------------------- 3. .env con secretos aleatorios
if [ ! -f .env ]; then
  say "Creando .env con secretos aleatorios..."
  cp .env.example .env
  python - <<'PY'
import re, secrets
from pathlib import Path
env = Path(".env"); s = env.read_text()
db_pw, jwt, admin_pw = secrets.token_hex(16), secrets.token_urlsafe(48), secrets.token_urlsafe(12)
def setv(s, k, v): return re.sub(rf"^{k}=.*$", f"{k}={v}", s, flags=re.M)
s = setv(s, "DATABASE_URL", f"postgresql+asyncpg://postgres:{db_pw}@127.0.0.1:5432/baas")
s = setv(s, "JWT_SECRET", jwt)
s = setv(s, "PLATFORM_ADMIN_PASSWORD", admin_pw)
env.write_text(s)
Path(".env").chmod(0o600)
print(f"\n  Usuario del panel : admin@example.com\n  Contraseña        : {admin_pw}\n  (están guardados en .env; cámbialos allí cuando quieras)\n")
PY
fi
load_env
DB_NAME="$(db_part db)"; DB_PASS="$(db_part password)"; DB_PORT="$(db_part port)"

# ---------------------------------------------------------------- 4. clúster PostgreSQL
if [ ! -f "$PGDATA/PG_VERSION" ]; then
  say "Inicializando PostgreSQL en $PGDATA ..."
  mkdir -p "$(dirname "$PGDATA")"
  PWFILE="$(mktemp)"; chmod 600 "$PWFILE"; printf '%s' "$DB_PASS" > "$PWFILE"
  # superusuario "postgres"; por socket local sin clave (solo tu usuario de Termux),
  # por TCP con contraseña.
  initdb -D "$PGDATA" -U postgres --pwfile="$PWFILE" --encoding=UTF8 --locale=C \
         --auth-local=trust --auth-host=scram-sha-256 >/dev/null
  rm -f "$PWFILE"
  cat >> "$PGDATA/postgresql.conf" <<CONF

# --- ajustes para un teléfono (baas_platform) ---
listen_addresses = '127.0.0.1'      # la base NO se expone a la red; solo la API habla con ella
port = $DB_PORT
unix_socket_directories = '${TMPDIR:-/tmp}'
max_connections = 40
shared_buffers = 64MB
work_mem = 4MB
maintenance_work_mem = 32MB
effective_cache_size = 256MB
wal_compression = on
max_wal_size = 256MB
synchronous_commit = on
CONF
fi

pg_start
export PGPASSWORD="$DB_PASS"
if ! psql -h 127.0.0.1 -p "$DB_PORT" -U postgres -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
  say "Creando la base de datos '$DB_NAME'..."
  createdb -h 127.0.0.1 -p "$DB_PORT" -U postgres "$DB_NAME"
fi

# ---------------------------------------------------------------- 5. migraciones
say "Aplicando migraciones..."
alembic upgrade head

say "Listo. Siguiente paso:  bash scripts/start.sh"
