#!/bin/bash
# Сбор статистики трафика клиентов в SQLite. Запуск каждые 5 минут.
set -euo pipefail

DB_DIR="/var/lib/amnezia-panel"
DB="$DB_DIR/traffic.db"
CONF="/etc/amnezia/amneziawg/awg1.conf"
RETAIN_DAYS=90

mkdir -p "$DB_DIR"
command -v sqlite3 >/dev/null || { echo "sqlite3 не установлен: apt install sqlite3"; exit 1; }

# Инициализация БД
if [[ ! -f "$DB" ]]; then
    sqlite3 "$DB" <<'SQL'
CREATE TABLE IF NOT EXISTS traffic (
    ts INTEGER NOT NULL,
    pubkey TEXT NOT NULL,
    name TEXT,
    rx_total INTEGER NOT NULL,
    tx_total INTEGER NOT NULL,
    PRIMARY KEY (ts, pubkey)
);
CREATE INDEX IF NOT EXISTS idx_traffic_name ON traffic(name, ts);
CREATE INDEX IF NOT EXISTS idx_traffic_ts   ON traffic(ts);
SQL
fi

TS=$(date +%s)

# Карта pubkey -> имя клиента
declare -A NAMES=()
while IFS= read -r block; do
    name=$(echo "$block" | grep -oP '#\s*Client:\s*\K\S+' | head -1 || true)
    pub=$(echo "$block" | grep -oP 'PublicKey\s*=\s*\K\S+' | head -1 || true)
    [[ -n "$name" && -n "$pub" ]] && NAMES[$pub]="$name"
done < <(awk 'BEGIN{RS="\\[Peer\\]"} NR>1' "$CONF")

# awg show awg1 dump → строки пиров
awg show awg1 dump | tail -n +2 | while IFS=$'\t' read -r pub psk ep allowed hs rx tx keepalive; do
    [[ -z "${pub:-}" || -z "${rx:-}" || -z "${tx:-}" ]] && continue
    name=${NAMES[$pub]:-unknown}
    sqlite3 "$DB" "INSERT OR REPLACE INTO traffic(ts,pubkey,name,rx_total,tx_total) \
        VALUES($TS,'$pub','$name',$rx,$tx);"
done

# Retention: раз в сутки (когда минуты 0-4)
if (( (TS % 86400) < 300 )); then
    sqlite3 "$DB" "DELETE FROM traffic WHERE ts < $((TS - RETAIN_DAYS*86400));"
fi