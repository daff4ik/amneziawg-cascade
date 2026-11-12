#!/bin/bash
# =====================================================================
#  stealth-setup.sh — Stealth-режим веб-панели:
#  сайт-заглушка + Nginx + Certbot + секретный путь к панели.
#  Единая реализация (вызывается вручную или из deploy.sh --update).
#
#  Свой сайт: положите файлы в /var/www/stealth ДО запуска —
#  тогда заглушка по умолчанию создаваться не будет.
# =====================================================================
set -euo pipefail

PANEL_CONF="/etc/amnezia-panel.conf"
STEALTH_CONF="/etc/amnezia-stealth.conf"
WEB_ROOT="/var/www/stealth"

[[ $EUID -ne 0 ]] && { echo "Ошибка: запускать от root (sudo $0)"; exit 1; }
[[ -f "$PANEL_CONF" ]] || { echo "Ошибка: веб-панель не установлена (нет $PANEL_CONF)"; exit 1; }

echo "=== Настройка Stealth-режима веб-панели ==="
read -p "Введите ваш домен или субдомен (например, mysite.com): " DOMAIN
[[ -z "$DOMAIN" ]] && { echo "Домен обязателен."; exit 1; }

read -p "Введите кодовое слово для скрытого пути (латиница/цифры, без слэшей): " SECRET_PATH
SECRET_PATH=$(echo "$SECRET_PATH" | tr -d '/' | sed 's/[^a-zA-Z0-9_-]//g')
[[ -z "$SECRET_PATH" ]] && { echo "Кодовое слово обязательно."; exit 1; }

# =====================================================================
# Сайт-заглушка (создаём только если пользователь не положил свой сайт)
# =====================================================================
if [[ ! -f "$WEB_ROOT/index.html" ]]; then
    echo "В $WEB_ROOT нет ваших файлов — создаю базовую ретро-заглушку."
    echo "Замените её на свой сайт позже (просто загрузите файлы в $WEB_ROOT)."
    mkdir -p "$WEB_ROOT"
    cat > "$WEB_ROOT/index.html" <<'EOF'
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SITE UNDER CONSTRUCTION</title>
    <link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap" rel="stylesheet">
    <style>
        body { background-color: #000; color: #fff; font-family: 'Press Start 2P', cursive, sans-serif; text-align: center; margin: 0; padding: 20px; overflow-x: hidden; }
        .score-board { display: flex; justify-content: space-between; color: #fff; font-size: 1.2rem; padding: 10px 5vw; }
        .score-board span { color: #f00; }
        h1 { color: #f00; margin-top: 5vh; font-size: 3rem; text-shadow: 4px 4px 0 #fff; line-height: 1.4; }
        .subtitle { color: #4ade80; font-size: 1.2rem; margin-bottom: 50px; }
        .game-container { border: 4px solid #fff; padding: 30px; margin: 0 auto; width: 80%; max-width: 800px; background-color: #111; box-shadow: 10px 10px 0 #333; }
        .game-container p { line-height: 2; font-size: 1rem; }
        .images { margin: 50px 0 20px 0; display: flex; justify-content: space-around; align-items: center; flex-wrap: wrap; gap: 20px; }
        .images img { height: 80px; image-rendering: pixelated; }
        .blink { animation: blinker 1s step-end infinite; color: #ffcc00; font-size: 1.5rem; margin-top: 60px; }
        @keyframes blinker { 50% { opacity: 0; } }
        .footer { margin-top: 60px; font-size: 0.8rem; color: #666; }
        @media (max-width: 600px) {
            h1 { font-size: 1.8rem; text-shadow: 2px 2px 0 #fff; }
            .score-board { font-size: 0.7rem; }
            .game-container { width: 90%; padding: 15px; }
            .images img { height: 50px; }
        }
    </style>
</head>
<body>
    <div class="score-board">
        <div>1UP<br><span>000000</span></div>
        <div>HIGH SCORE<br><span>999999</span></div>
    </div>
    <h1>САЙТ В РАЗРАБОТКЕ</h1>
    <div class="subtitle">STAGE 1 - UNDER CONSTRUCTION</div>
    <div class="game-container">
        <p>РАЗРАБОТКА ИДЕТ ПОЛНЫМ ХОДОМ...<br><br>НО ПРИНЦЕССА В ДРУГОМ ЗАМКЕ!</p>
        <div class="images">
            <img src="https://i.gifer.com/4l3.gif" alt="Mario">
            <img src="https://i.gifer.com/1YZ.gif" alt="Battle City">
            <img src="https://i.gifer.com/3Q0.gif" alt="Contra" style="height: 100px;">
        </div>
    </div>
    <div class="blink">INSERT COIN TO CONTINUE_</div>
    <div class="footer">© 2026 NINTENDO / KONAMI / NAMCO VISUALS</div>
</body>
</html>
EOF
    chmod -R 755 "$WEB_ROOT"
else
    echo "Найден ваш сайт в $WEB_ROOT — использую его как заглушку."
fi

# =====================================================================
# Nginx + Certbot
# =====================================================================
echo "Установка Nginx и Certbot..."
apt update -qq
apt install -y -qq nginx certbot python3-certbot-nginx

cat > /etc/nginx/sites-available/amnezia-stealth <<EOF
server {
    listen 80;
    server_name $DOMAIN;

    root $WEB_ROOT;
    index index.html index.htm;

    location / {
        try_files \$uri \$uri/ =404;
    }

    # Строгий редирект для секретного пути (добавляет слэш на конце, если его нет)
    location = /$SECRET_PATH {
        return 301 /$SECRET_PATH/;
    }

    # Проксирование панели Amnezia
    location /$SECRET_PATH/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # Поддержка WebSocket (на будущее)
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
EOF

ln -sf /etc/nginx/sites-available/amnezia-stealth /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl restart nginx

echo "Получение SSL-сертификата Let's Encrypt для $DOMAIN..."
if certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
        --register-unsafely-without-email --redirect; then
    echo "✅ SSL сертификат получен успешно!"
else
    echo "⚠️ Не удалось получить SSL. Убедитесь, что DNS-запись указывает на этот сервер."
    echo "⚠️ Сайт и панель пока будут доступны по HTTP (порт 80)."
fi

# =====================================================================
# Замыкание панели локально + сохранение параметров stealth
# =====================================================================
sed -i '/^host=/d; /^ssl=/d; /^base_url=/d' "$PANEL_CONF"
{
    echo "host=127.0.0.1"
    echo "ssl=false"
    echo "base_url=/$SECRET_PATH"
} >> "$PANEL_CONF"

cat > "$STEALTH_CONF" <<EOF
domain=$DOMAIN
secret_path=$SECRET_PATH
EOF
chmod 600 "$STEALTH_CONF"

systemctl restart amnezia-panel

echo "=============================================="
echo "✅ Stealth-режим успешно активирован!"
echo "=============================================="
echo "🌍 Заглушка: https://$DOMAIN"
echo "🔐 Панель:   https://$DOMAIN/$SECRET_PATH/"
echo "=============================================="
echo "Прямой доступ по IP заблокирован. Если понадобится вернуть"
echo "доступ по IP (например, при сбое домена), используйте скрипт:"
echo "sudo amnezia-panel-access.sh on"
