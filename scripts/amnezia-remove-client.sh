#!/bin/bash
# =====================================================================
#  amnezia-remove-client.sh
#  Удаляет клиента AmneziaWG: peer из awg1.conf, .conf и .vpn.txt.
# =====================================================================
set -euo pipefail

AWG_DIR="/etc/amnezia/amneziawg"
AWG_IFACE="awg1"
CLIENTS_DIR="/root/clients"
SERVER_CONF="$AWG_DIR/$AWG_IFACE.conf"

[[ $EUID -ne 0 ]] && { echo "Ошибка: запускать от root (sudo $0)"; exit 1; }
[[ ! -f "$SERVER_CONF" ]] && { echo "Ошибка: файл $SERVER_CONF не найден"; exit 1; }

CLIENT_NAME=""

# === Выбор клиента: из аргумента или через меню ===
if [[ $# -ge 1 ]]; then
    CLIENT_NAME="$1"
else
    mapfile -t CLIENT_LIST < <(grep -E '^# Client:' "$SERVER_CONF" | sed 's/^# Client:[[:space:]]*//' | sed 's/[^a-zA-Z0-9_-]//g' | grep -v '^$' || true)

    if [[ ${#CLIENT_LIST[@]} -eq 0 ]]; then
        echo "В $SERVER_CONF не найдено ни одного клиента."
        exit 0
    fi

    echo "=============================================="
    echo " Список активных клиентов AmneziaWG:"
    echo "=============================================="
    for i in "${!CLIENT_LIST[@]}"; do
        printf " %2d) %s\n" "$((i + 1))" "${CLIENT_LIST[$i]}"
    done
    echo "=============================================="
    echo ""
    read -p "Выберите номер клиента для удаления (1-${#CLIENT_LIST[@]}) или 'q' для выхода: " CHOICE

    if [[ "$CHOICE" =~ ^[qQ]$ ]]; then
        echo "Отмена."
        exit 0
    fi

    if [[ ! "$CHOICE" =~ ^[0-9]+$ ]] || (( CHOICE < 1 || CHOICE > ${#CLIENT_LIST[@]} )); then
        echo "Ошибка: некорректный выбор."
        exit 1
    fi

    CLIENT_NAME="${CLIENT_LIST[$((CHOICE - 1))]}"
fi

CLIENT_SAFE=$(echo "$CLIENT_NAME" | sed 's/[^a-zA-Z0-9_-]//g')
[[ -z "$CLIENT_SAFE" ]] && { echo "Ошибка: некорректное имя клиента"; exit 1; }

if ! grep -q "# Client: $CLIENT_SAFE" "$SERVER_CONF"; then
    echo "Ошибка: клиент '$CLIENT_SAFE' не найден в $SERVER_CONF"
    exit 1
fi

read -p "Вы уверены, что хотите полностью удалить клиента '$CLIENT_SAFE'? (y/n): " CONFIRM
case "$CONFIRM" in
    [yY]|[yY][eE][sS]|[дД]|[дД][aA]) ;;
    *) echo "Удаление отменено."; exit 0 ;;
esac

echo ""
echo "Удаляем клиента '$CLIENT_SAFE'..."

# === 1. Удаление блока [Peer] через Python ===
python3 - "$SERVER_CONF" "$CLIENT_SAFE" <<'PYEOF'
import sys, re

conf_path = sys.argv[1]
client_name = sys.argv[2]

with open(conf_path, 'r', encoding='utf-8') as f:
    content = f.read()

pattern = rf'\n*\[Peer\]\s*\n\s*#\s*Client:\s*{re.escape(client_name)}\b.*?(?=\n\[Peer\]|\Z)'
new_content = re.sub(pattern, '', content, flags=re.DOTALL)

with open(conf_path, 'w', encoding='utf-8') as f:
    f.write(new_content)
PYEOF

# === 2. Удаление файлов конфига и ссылки ===
CLIENT_CONF="$CLIENTS_DIR/${CLIENT_SAFE}.conf"
CLIENT_VPN="$CLIENTS_DIR/${CLIENT_SAFE}.vpn.txt"

if [[ -f "$CLIENT_CONF" ]]; then
    rm -f "$CLIENT_CONF"
    echo "Файл конфига удалён: $CLIENT_CONF"
else
    echo "Файл конфига не найден: $CLIENT_CONF"
fi

if [[ -f "$CLIENT_VPN" ]]; then
    rm -f "$CLIENT_VPN"
    echo "Файл ссылки удалён: $CLIENT_VPN"
fi

# === 3. Перезапуск ===
systemctl restart "awg-quick@$AWG_IFACE"
echo "Интерфейс $AWG_IFACE перезапущен."

echo ""
echo "=============================================="
echo " Клиент '$CLIENT_SAFE' успешно удалён!"
echo "=============================================="