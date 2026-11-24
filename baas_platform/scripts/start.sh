#!/usr/bin/env bash
# Arranca PostgreSQL y la API (con workers de webhooks y backups dentro del mismo proceso).
#   bash scripts/start.sh            # en segundo plano, logs en logs/api.log
#   bash scripts/start.sh --fg       # en primer plano (Ctrl+C para parar)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
cd "$BAAS_DIR"
load_env
# shellcheck disable=SC1091
source "$VENV/bin/activate" 2>/dev/null || die "Falta el entorno virtual. Ejecuta:  bash scripts/install.sh"

if pid="$(api_pid)"; then die "La API ya está en marcha (PID $pid). Usa scripts/stop.sh o scripts/status.sh"; fi

# Evita que Android duerma la CPU mientras el servidor corre (comando de termux-tools).
command -v termux-wake-lock >/dev/null && termux-wake-lock || warn "Sin termux-wake-lock: Android podría suspender el servidor con la pantalla apagada."

pg_start
python -m alembic upgrade head >/dev/null   # aplica migraciones nuevas si las hay

HOST="${HOST:-0.0.0.0}"; PORT="${PORT:-8000}"
IP="$(lan_ip)"
say "Panel:  http://$IP:$PORT/dashboard   (desde el propio teléfono: http://127.0.0.1:$PORT/dashboard)"

if [ "${1:-}" = "--fg" ]; then
  exec python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
fi
nohup python -m uvicorn app.main:app --host "$HOST" --port "$PORT" >>"$LOG_DIR/api.log" 2>&1 &
echo $! > "$API_PID"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then say "API en marcha (PID $(cat "$API_PID"))."; exit 0; fi
  kill -0 "$(cat "$API_PID")" 2>/dev/null || { tail -n 25 "$LOG_DIR/api.log" >&2; rm -f "$API_PID"; die "La API se cerró al arrancar."; }
  sleep 1
done
warn "La API no respondió en 30 s; revisa logs/api.log"
