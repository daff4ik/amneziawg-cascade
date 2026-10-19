#!/bin/bash
# =====================================================================
#  amnezia-update.sh
#  Обновляет helper-скрипты и веб-панель. Не трогает awg0/awg1/SERVER-B.
#  Запускается по кнопке "Update" в панели или вручную.
# =====================================================================
set -euo pipefail

LOG_FILE="/var/log/amnezia-update.log"
exec >> "$LOG_FILE" 2>&1
echo ""
echo "=== Update started at $(date '+%F %T') ==="

REPO_URL="https://raw.githubusercontent.com/davideprikyan/amneziawg-cascade/main"

# Сохраняем текущий SERVER_LABEL из существующей панели
SERVER_LABEL="AmneziaVPN"
if [[ -f /opt/amnezia-panel.py ]]; then
    LABEL=$(grep -oP 'SERVER_LABEL\s*=\s*"\K[^"]+' /opt/amnezia-panel.py | head -1 || true)
    [[ -n "${LABEL:-}" ]] && SERVER_LABEL="$LABEL"
fi

echo "REPO_URL=$REPO_URL"
echo "SERVER_LABEL=$SERVER_LABEL"

# Удаляем устаревший bash-collector, если остался от прошлых версий
rm -f /usr/local/bin/amnezia-traffic-collector.sh

# Обновляем helper-скрипты
SCRIPTS=(
    "amnezia-add-client.sh"
    "amnezia-remove-client.sh"
    "amnezia-list-clients.sh"
    "amnezia-notify.sh"
    "amnezia-notify-setup.sh"
    "amnezia-healthcheck.sh"
    "amnezia-traffic-collector.py"
    "amnezia-update.sh"
)

for f in "${SCRIPTS[@]}"; do
    echo "  Downloading $f..."
    if curl -fsSL "$REPO_URL/scripts/$f" -o "/usr/local/bin/$f"; then
        chmod +x "/usr/local/bin/$f"
        echo "    OK"
    else
        echo "    FAILED"
    fi
done

# Обновляем панель
echo "Updating panel..."
if curl -fsSL "$REPO_URL/web-panel/amnezia-panel.py" -o /tmp/amnezia-panel.py; then
    sed "s|__SERVER_LABEL__|$SERVER_LABEL|g" /tmp/amnezia-panel.py > /opt/amnezia-panel.py
    chmod 700 /opt/amnezia-panel.py
    rm -f /tmp/amnezia-panel.py
    echo "  Panel updated"
else
    echo "  Panel download FAILED"
fi

# Обновляем зависимости (на случай новых пакетов)
/opt/amnezia-panel-venv/bin/pip install --quiet fastapi 'uvicorn[standard]' python-multipart qrcode 2>&1 || true

# Перезапускаем панель
echo "Restarting panel..."
systemctl restart amnezia-panel || true
sleep 2

if systemctl is-active --quiet amnezia-panel; then
    echo "=== Update finished OK at $(date '+%F %T') ==="
else
    echo "=== Update FAILED - panel not active ==="
fi