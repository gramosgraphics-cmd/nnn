#!/usr/bin/env bash
# Backup manual completo: pg_dump de toda la base + copia de los archivos subidos, en un .tar.gz.
#   bash scripts/backup_now.sh [carpeta-destino]    (por ejemplo ~/storage/shared/Download)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
cd "$BAAS_DIR"; load_env
OUT="${1:-$BAAS_DIR/backups}"; mkdir -p "$OUT"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"; TMP="$(mktemp -d)"
pg_start
PGPASSWORD="$(db_part password)" pg_dump -h 127.0.0.1 -p "$(db_part port)" -U "$(db_part user)" -d "$(db_part db)" --format=custom -f "$TMP/database.dump"
[ -d "${STORAGE_DIR:-./storage_data}" ] && cp -r "${STORAGE_DIR:-./storage_data}" "$TMP/storage_data"
tar -czf "$OUT/baas_full_$STAMP.tar.gz" -C "$TMP" .
rm -rf "$TMP"
say "Backup creado: $OUT/baas_full_$STAMP.tar.gz"
