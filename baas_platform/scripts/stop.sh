#!/usr/bin/env bash
# Detiene la API y PostgreSQL (con apagado ordenado, sin corromper datos).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
if pid="$(api_pid)"; then
  say "Deteniendo la API (PID $pid)..."
  kill "$pid"
  for _ in $(seq 1 15); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid" || true
fi
rm -f "$API_PID"
pg_stop
command -v termux-wake-unlock >/dev/null && termux-wake-unlock || true
say "Detenido."
