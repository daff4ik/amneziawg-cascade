#!/bin/bash
# Отправка уведомления в Telegram
# Использование: amnezia-notify.sh "текст"
set -euo pipefail

CONFIG_FILE="/etc/amnezia-notify.conf"
LOG_FILE="/var/log/amnezia-notify.log"

[[ ! -f "$CONFIG_FILE" ]] && { echo "Нет $CONFIG_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
source "$CONFIG_FILE"

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]] || [[ -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    echo "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы" >&2
    exit 1
fi

[[ $# -lt 1 ]] && { echo "Использование: $0 <текст>" >&2; exit 1; }

MESSAGE="$*"

RESPONSE=$(curl -sS --max-time 10 -X POST \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" \
    -d "parse_mode=HTML" \
    --data-urlencode "text=${MESSAGE}" 2>&1) || {
    echo "[$(date '+%F %T')] NETWORK ERROR: $RESPONSE" >> "$LOG_FILE"
    exit 1
}

if ! echo "$RESPONSE" | grep -q '"ok":true'; then
    echo "[$(date '+%F %T')] API ERROR: $RESPONSE" >> "$LOG_FILE"
    exit 1
fi

echo "[$(date '+%F %T')] SENT: $MESSAGE" >> "$LOG_FILE"