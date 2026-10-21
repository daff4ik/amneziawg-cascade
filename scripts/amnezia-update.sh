#!/bin/bash
# =====================================================================
#  amnezia-update.sh
#  Обновляет helper-скрипты и веб-панель. Не трогает awg0/awg1/SERVER-B.
# =====================================================================
set -euo pipefail

LOG_FILE="/var/log/amnezia-update.log"
exec >> "$LOG_FILE" 2>&1
echo ""
echo "=== Update started at $(date '+%F %T') ==="

REPO_URL="https://raw.githubusercontent.com/daff4ik/amneziawg-cascade/main"

# --- Сохраняем текущие значения из установленного amnezia-add-client.sh ---
OLD_ENDPOINT=""
OLD_NAME=""
OLD_MTU=""
OLD_DNS=""
if [[ -f /usr/local/bin/amnezia-add-client.sh ]]; then
    OLD_ENDPOINT=$(grep -oP '^SERVER_ENDPOINT="\K[^"]+' /usr/local/bin/amnezia-add-client.sh 2>/dev/null || true)
    OLD_NAME=$(grep -oP '^SERVER_NAME="\K[^"]+' /usr/local/bin/amnezia-add-client.sh 2>/dev/null || true)
    OLD_MTU=$(grep -oP '^CLIENT_MTU="\K[^"]+' /usr/local/bin/amnezia-add-client.sh 2>/dev/null || true)
    OLD_DNS=$(grep -oP '^DNS_SERVERS="\K[^"]+' /usr/local/bin/amnezia-add-client.sh 2>/dev/null || true)
fi

# Резервные значения, если не удалось прочитать
[[ -z "$OLD_ENDPOINT" || "$OLD_ENDPOINT" == "__SERVER_ENDPOINT__" ]] && OLD_ENDPOINT="$(hostname -I | awk '{print $1}'):585"
[[ -z "$OLD_NAME" || "$OLD_NAME" == "__SERVER_NAME__" ]] && OLD_NAME="AmneziaVPN"
[[ -z "$OLD_MTU" || "$OLD_MTU" == "__CLIENT_MTU__" ]] && OLD_MTU="1280"
[[ -z "$OLD_DNS" || "$OLD_DNS" == "__DNS_SERVERS__" ]] && OLD_DNS="1.1.1.1, 1.0.0.1"

echo "Preserved values: endpoint=$OLD_ENDPOINT name=$OLD_NAME mtu=$OLD_MTU dns=$OLD_DNS"

# Сохраняем текущий SERVER_LABEL из существующей панели
SERVER_LABEL="$OLD_NAME"
if [[ -f /opt/amnezia-panel.py ]]; then
    LABEL=$(grep -oP 'SERVER_LABEL\s*=\s*"\K[^"]+' /opt/amnezia-panel.py | head -1 || true)
    [[ -n "${LABEL:-}" && "$LABEL" != "__SERVER_LABEL__" ]] && SERVER_LABEL="$LABEL"
fi

echo "SERVER_LABEL=$SERVER_LABEL"

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
    if curl -fsSL "$REPO_URL/scripts/$f" -o "/tmp/$f"; then
        if [[ "$f" == "amnezia-add-client.sh" ]]; then
            # Подставляем сохранённые значения
            sed -e "s|__SERVER_ENDPOINT__|$OLD_ENDPOINT|g" \
                -e "s|__SERVER_NAME__|$OLD_NAME|g" \
                -e "s|__CLIENT_MTU__|$OLD_MTU|g" \
                -e "s|__DNS_SERVERS__|$OLD_DNS|g" \
                "/tmp/$f" > "/usr/local/bin/$f"
        else
            cp "/tmp/$f" "/usr/local/bin/$f"
        fi
        chmod +x "/usr/local/bin/$f"
        rm -f "/tmp/$f"
        echo "    OK"
    else
        echo "    FAILED (skipping)"
    fi
done

# --- Проверка: не осталось ли плейсхолдеров ---
echo "Verifying substitutions..."
for f in /usr/local/bin/amnezia-*.sh; do
    if grep -qE '__[A-Z_]+__' "$f"; then
        echo "  WARNING: $f has placeholders:"
        grep -oE '__[A-Z_]+__' "$f" | sort -u | sed 's/^/    /'
    fi
done

# Обновляем панель
echo "Updating panel..."
if curl -fsSL "$REPO_URL/web-panel/amnezia-panel.py" -o /tmp/amnezia-panel.py; then
    sed "s|__SERVER_LABEL__|$SERVER_LABEL|g" /tmp/amnezia-panel.py > /opt/amnezia-panel.py
    chmod 700 /opt/amnezia-panel.py
    rm -f /tmp/amnezia-panel.py
    echo "  Panel updated"
fi

/opt/amnezia-panel-venv/bin/pip install --quiet fastapi 'uvicorn[standard]' python-multipart qrcode 2>&1 || true

echo "Restarting panel..."
systemctl restart amnezia-panel || true
sleep 2

if systemctl is-active --quiet amnezia-panel; then
    echo "=== Update finished OK at $(date '+%F %T') ==="
else
    echo "=== Update FAILED - panel not active ==="
fi