#!/bin/bash
CONF="/etc/amnezia-panel.conf"

if [[ "$1" == "on" ]]; then
    sed -i 's/^host=.*/host=0.0.0.0/' "$CONF"
    sed -i 's/^ssl=.*/ssl=true/' "$CONF"
    sed -i 's|^base_url=.*|base_url=|' "$CONF"
    systemctl restart amnezia-panel
    echo "⚠️ Прямой доступ по IP:8000 (HTTPS) ВКЛЮЧЕН."
    echo "Stealth-маршрутизация Nginx отключена."
elif [[ "$1" == "off" ]]; then
    sed -i 's/^host=.*/host=127.0.0.1/' "$CONF"
    sed -i 's/^ssl=.*/ssl=false/' "$CONF"
    
    # Пытаемся вспомнить секретный путь из Nginx конфигов
    SECRET=$(grep -h "location /" /etc/nginx/sites-enabled/* 2>/dev/null | grep -v "location / {" | awk -F'/' '{print $2}' | head -1)
    [[ -n "$SECRET" ]] && sed -i "s|^base_url=.*|base_url=/$SECRET|" "$CONF"
    
    systemctl restart amnezia-panel
    echo "✅ Прямой доступ ОТКЛЮЧЕН. Панель работает только через Nginx Stealth."
else
    echo "Использование: sudo amnezia-panel-access.sh [on | off]"
fi