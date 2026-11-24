#!/usr/bin/env bash
# Arranque automático al encender el teléfono (requiere la app "Termux:Boot" de F-Droid, abierta una vez).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/baas.sh" <<BOOT
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
sleep 15
bash "$BAAS_DIR/scripts/start.sh"
BOOT
chmod +x "$HOME/.termux/boot/baas.sh"
say "Listo: $HOME/.termux/boot/baas.sh. Abre Termux:Boot una vez y desactiva el ahorro de batería para Termux."
