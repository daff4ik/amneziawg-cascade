#!/bin/bash
# =====================================================================
#  amnezia-list-clients.sh
#  Показывает таблицу всех клиентов AmneziaWG с трафиком и статусом.
# =====================================================================
set -euo pipefail

AWG_DIR="/etc/amnezia/amneziawg"
AWG_IFACE="awg1"
CLIENTS_DIR="/root/clients"
SERVER_CONF="$AWG_DIR/$AWG_IFACE.conf"

[[ $EUID -ne 0 ]] && { echo "Запускать от root (sudo $0)"; exit 1; }
[[ ! -f "$SERVER_CONF" ]] && { echo "Не найден $SERVER_CONF"; exit 1; }
command -v awg >/dev/null || { echo "Не найден awg"; exit 1; }

# =====================================================
#  ЭТАП 1. Собираем список клиентов из awg1.conf
# =====================================================
declare -a NAMES=() IPS=() PUBKEYS=()

in_peer=0
name=""; ip=""; pub=""

while IFS= read -r line; do
    if [[ "$line" == "[Peer]"* ]]; then
        if [[ $in_peer -eq 1 && -n "$name" ]]; then
            NAMES+=("$name"); IPS+=("$ip"); PUBKEYS+=("$pub")
        fi
        in_peer=1; name=""; ip=""; pub=""
    elif [[ "$line" == "[Interface]"* ]]; then
        if [[ $in_peer -eq 1 && -n "$name" ]]; then
            NAMES+=("$name"); IPS+=("$ip"); PUBKEYS+=("$pub")
        fi
        in_peer=0; name=""; ip=""; pub=""
    elif [[ $in_peer -eq 1 && "$line" =~ ^#?[[:space:]]*Client:[[:space:]]*(.+)$ ]]; then
        name="${BASH_REMATCH[1]}"
    elif [[ $in_peer -eq 1 && "$line" =~ ^PublicKey[[:space:]]*=[[:space:]]*(.+)$ ]]; then
        pub="${BASH_REMATCH[1]}"
    elif [[ $in_peer -eq 1 && "$line" =~ ^AllowedIPs[[:space:]]*=[[:space:]]*([0-9.]+) ]]; then
        ip="${BASH_REMATCH[1]}"
    fi
done < "$SERVER_CONF"

if [[ $in_peer -eq 1 && -n "$name" ]]; then
    NAMES+=("$name"); IPS+=("$ip"); PUBKEYS+=("$pub")
fi

if [[ ${#NAMES[@]} -eq 0 ]]; then
    echo "Клиентов не найдено в $SERVER_CONF"
    exit 0
fi

# =====================================================
#  ЭТАП 2. Собираем статистику из awg show awg1
# =====================================================
declare -A HS=() RX=() TX=()

current=""
while IFS= read -r line; do
    if [[ "$line" =~ ^peer:[[:space:]]+(.+)$ ]]; then
        current="${BASH_REMATCH[1]}"
        HS[$current]="never"
        RX[$current]="0 B"
        TX[$current]="0 B"
    elif [[ -n "$current" ]]; then
        if [[ "$line" =~ latest[[:space:]]+handshake:[[:space:]]+(.+)$ ]]; then
            HS[$current]="${BASH_REMATCH[1]}"
        elif [[ "$line" =~ transfer:[[:space:]]+(.+)[[:space:]]+received,[[:space:]]+(.+)[[:space:]]+sent ]]; then
            RX[$current]="${BASH_REMATCH[1]}"
            TX[$current]="${BASH_REMATCH[2]}"
        fi
    fi
done < <(awg show "$AWG_IFACE" 2>/dev/null || true)

# =====================================================
#  ЭТАП 3. Печатаем таблицу
# =====================================================
echo ""
echo "=== Клиенты AmneziaWG ($AWG_IFACE) ==="
printf "%-18s %-15s %-15s %-13s %-13s %-8s\n" \
    "NAME" "IP" "HANDSHAKE" "RECEIVED" "SENT" "STATUS"
echo "----------------------------------------------------------------------------------------------"

online=0; offline=0; never=0
for i in "${!NAMES[@]}"; do
    name="${NAMES[$i]}"
    ip="${IPS[$i]}"
    pub="${PUBKEYS[$i]}"

    hs="${HS[$pub]:-never}"
    rx="${RX[$pub]:-0 B}"
    tx="${TX[$pub]:-0 B}"

    status="OFFLINE"
    if [[ "$hs" == "never" ]]; then
        status="NEVER"; never=$((never + 1))
    elif [[ "$hs" =~ ^([0-9]+)[[:space:]]+seconds ]]; then
        status="ONLINE"; online=$((online + 1))
    elif [[ "$hs" =~ ^([0-9]+)[[:space:]]+minutes? ]]; then
        if (( BASH_REMATCH[1] < 3 )); then
            status="ONLINE"; online=$((online + 1))
        else
            offline=$((offline + 1))
        fi
    else
        offline=$((offline + 1))
    fi

    printf "%-18s %-15s %-15s %-13s %-13s %-8s\n" \
        "$name" "$ip" "$hs" "$rx" "$tx" "$status"
done

echo "----------------------------------------------------------------------------------------------"
echo "Всего: ${#NAMES[@]} | ONLINE: $online | OFFLINE: $offline | NEVER: $never"
echo "Конфиги: $CLIENTS_DIR"
echo ""