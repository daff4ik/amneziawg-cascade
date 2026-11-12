#!/bin/bash
# =====================================================================
#  amnezia-panel-access.sh — аварийное управление доступом к веб-панели.
#    on  — открыть прямой доступ по IP:8000 (HTTPS), в обход Stealth
#    off — вернуть панель за Nginx (только 127.0.0.1 + секретный путь)
# =====================================================================
set -euo pipefail

CONF="/etc/amnezia-panel.conf"
STEALTH_CONF="/etc/amnezia-stealth.conf"

[[ $EUID -ne 0 ]] && { echo "Ошибка: запускать от root (sudo $0)"; exit 1; }
[[ -f "$CONF" ]] || { echo "Ошибка: веб-панель не установлена (нет $CONF)"; exit 1; }

case "${1:-}" in
    on)
        sed -i 's/^host=.*/host=0.0.0.0/' "$CONF"
        sed -i 's/^ssl=.*/ssl=true/' "$CONF"
        sed -i 's|^base_url=.*|base_url=|' "$CONF"
        systemctl restart amnezia-panel
        echo "⚠️ Прямой доступ по IP:8000 (HTTPS) ВКЛЮЧЕН."
        echo "Stealth-маршрутизация Nginx отключена."
        ;;
    off)
        [[ -f "$STEALTH_CONF" ]] || {
            echo "Ошибка: нет $STEALTH_CONF — Stealth не настроен."
            echo "Сначала запустите stealth-setup.sh (или deploy.sh --update)."
            exit 1
        }
        SECRET=$(awk -F'=' '/^secret_path/ {print $2}' "$STEALTH_CONF" | tr -d ' \r\n')
        [[ -n "$SECRET" ]] || { echo "Ошибка: пустой secret_path в $STEALTH_CONF"; exit 1; }
        sed -i 's/^host=.*/host=127.0.0.1/' "$CONF"
        sed -i 's/^ssl=.*/ssl=false/' "$CONF"
        sed -i "s|^base_url=.*|base_url=/$SECRET|" "$CONF"
        systemctl restart amnezia-panel
        echo "✅ Прямой доступ ОТКЛЮЧЕН. Панель работает только через Nginx Stealth."
        ;;
    *)
        echo "Использование: sudo amnezia-panel-access.sh [on | off]"
        ;;
esac
