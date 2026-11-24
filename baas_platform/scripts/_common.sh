# Variables y funciones compartidas por los scripts (se carga con `source`, no se ejecuta).
# Todo se puede sobreescribir con variables de entorno (útil para probar fuera de Termux).

BAAS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"

PGDATA="${PGDATA:-$PREFIX/var/lib/postgresql}"
PGLOG="${PGLOG:-$BAAS_DIR/logs/postgres.log}"
RUN_DIR="$BAAS_DIR/.run"
LOG_DIR="$BAAS_DIR/logs"
API_PID="$RUN_DIR/api.pid"
VENV="$BAAS_DIR/.venv"
mkdir -p "$RUN_DIR" "$LOG_DIR"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

is_termux() { [ -n "${TERMUX_VERSION:-}" ] || [ -d /data/data/com.termux ]; }

load_env() {
  [ -f "$BAAS_DIR/.env" ] || die "No existe .env. Ejecuta primero:  bash scripts/install.sh"
  set -a; . "$BAAS_DIR/.env"; set +a
}

# Lee un valor de DATABASE_URL (user, password, host, port, db) sin depender de librerías externas.
db_part() {
  python3 - "$1" "$DATABASE_URL" <<'PY'
import sys
from urllib.parse import urlsplit, unquote
part, url = sys.argv[1], sys.argv[2]
u = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://", 1))
print({"user": unquote(u.username or "postgres"), "password": unquote(u.password or ""),
       "host": u.hostname or "127.0.0.1", "port": u.port or 5432, "db": (u.path or "/postgres").lstrip("/")}[part])
PY
}

pg_running() { pg_ctl -D "$PGDATA" status >/dev/null 2>&1; }

pg_start() {
  if pg_running; then return 0; fi
  [ -d "$PGDATA" ] || die "No hay clúster de PostgreSQL en $PGDATA. Ejecuta:  bash scripts/install.sh"
  say "Arrancando PostgreSQL..."
  pg_ctl -D "$PGDATA" -l "$PGLOG" -w -t 60 start >/dev/null || { tail -n 20 "$PGLOG" >&2; die "PostgreSQL no arrancó (log: $PGLOG)"; }
}

pg_stop() { pg_running && { say "Deteniendo PostgreSQL..."; pg_ctl -D "$PGDATA" -m fast -w stop >/dev/null; } || true; }

api_pid() { [ -f "$API_PID" ] && kill -0 "$(cat "$API_PID")" 2>/dev/null && cat "$API_PID"; }

lan_ip() {
  python3 - <<'PY' 2>/dev/null || echo "IP-DEL-TELEFONO"
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.connect(("8.8.8.8", 80))  # UDP: no envía nada, solo elige la interfaz
print(s.getsockname()[0])
PY
}
