#!/bin/bash
# Проверка туннеля awg0. Запуск раз в час через cron.
set -uo pipefail

TUNNEL_IP="10.10.10.1"
MAX_HANDSHAKE_AGE=600
STATE_FILE="/var/lib/amnezia-healthcheck/state"
LOG="/var/log/amnezia-healthcheck.log"
NOTIFY="/usr/local/bin/amnezia-notify.sh"

mkdir -p "$(dirname "$STATE_FILE")"
log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }
notify() { [[ -x "$NOTIFY" ]] && "$NOTIFY" "$1" >/dev/null 2>&1 || true; }
was_down() { [[ -f "$STATE_FILE" ]] && grep -q "DOWN" "$STATE_FILE"; }
mark_down() { echo "DOWN $(date +%s)" > "$STATE_FILE"; }
mark_up()   { echo "UP $(date +%s)"   > "$STATE_FILE"; }

svc=$(systemctl is-active awg-quick@awg0 2>/dev/null || echo inactive)

ping_ok=0
ping -c 3 -W 3 "$TUNNEL_IP" >/dev/null 2>&1 && ping_ok=1

hs_ok=0; age=999999
hs=$(awg show awg0 latest-handshakes 2>/dev/null | awk 'NR==1{print $2}')
now=$(date +%s)
if [[ -n "$hs" && "$hs" != "0" ]]; then
    age=$(( now - hs ))
    (( age <= MAX_HANDSHAKE_AGE )) && hs_ok=1
fi

if [[ "$svc" == "active" && $ping_ok -eq 1 && $hs_ok -eq 1 ]]; then
    if was_down; then
        mark_up
        notify "✅ <b>Туннель awg0 восстановлен</b>
Хост: <code>$(hostname)</code>
Ping: OK, handshake: ${age}s
Время: $(date '+%F %T')"
        log "RECOVERED"
    fi
    log "OK (hs=${age}s)"
    exit 0
fi

log "PROBLEM svc=$svc ping=$ping_ok hs=$hs_ok age=$age"

if ! was_down; then
    mark_down
    notify "⚠️ <b>Туннель awg0 проблемный</b>
Хост: <code>$(hostname)</code>
Сервис: $svc
Ping: $([[ $ping_ok -eq 1 ]] && echo OK || echo FAIL)
Handshake: $([[ $hs_ok -eq 1 ]] && echo OK || echo FAIL) (${age}s)
Перезапускаю awg0..."
fi

systemctl restart awg-quick@awg0
sleep 5

if ping -c 3 -W 3 "$TUNNEL_IP" >/dev/null 2>&1; then
    mark_up
    notify "✅ <b>Туннель awg0 восстановлен после перезапуска</b>
Хост: <code>$(hostname)</code>
Время: $(date '+%F %T')"
    log "RECOVERED after restart"
    exit 0
else
    notify "❌ <b>Туннель awg0 не восстанавливается</b>
Хост: <code>$(hostname)</code>
Перезапуск не помог. Требуется ручное вмешательство.
Время: $(date '+%F %T')"
    log "STILL DOWN"
    exit 1
fi