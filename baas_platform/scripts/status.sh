#!/usr/bin/env bash
# Muestra el estado de PostgreSQL y la API, la URL del panel y el espacio usado.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
cd "$BAAS_DIR"
[ -f .env ] && load_env
PORT="${PORT:-8000}"
pg_running && echo "PostgreSQL : en marcha" || echo "PostgreSQL : detenido"
if pid="$(api_pid)"; then echo "API        : en marcha (PID $pid)"; else echo "API        : detenida"; fi
if curl -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null; then echo; else echo "Health     : sin respuesta"; fi
echo "Panel      : http://$(lan_ip):$PORT/dashboard"
du -sh "${STORAGE_DIR:-$BAAS_DIR/storage_data}" 2>/dev/null | awk '{print "Archivos    : " $1}'
du -sh "$PGDATA" 2>/dev/null | awk '{print "Base datos  : " $1}'
du -sh "${BACKUP_DIR:-$BAAS_DIR/backups}" 2>/dev/null | awk '{print "Backups     : " $1}' || true
