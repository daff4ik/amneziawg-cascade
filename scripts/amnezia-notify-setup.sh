#!/bin/bash
# Интерактивная настройка Telegram-уведомлений
set -euo pipefail

CONFIG="/etc/amnezia-notify.conf"
NOTIFY="/usr/local/bin/amnezia-notify.sh"

echo "=============================================="
echo " Настройка Telegram-уведомлений"
echo "=============================================="
echo ""
echo "1) Создайте бота: https://t.me/BotFather"
echo "   Отправьте /newbot, придумайте имя."
echo "   BotFather выдаст токен вида 123456789:AAAbbbCCC..."
echo ""
echo "2) Узнайте свой chat_id: напишите боту любое сообщение,"
echo "   затем откройте: https://api.telegram.org/bot<TOKEN>/getUpdates"
echo "   В поле 'chat' → 'id' будет ваш chat_id (для группы — отрицательный)."
echo ""

read -p "Введите TELEGRAM_BOT_TOKEN: " TOKEN
[[ -z "$TOKEN" ]] && { echo "Ошибка: токен пуст"; exit 1; }

read -p "Введите TELEGRAM_CHAT_ID: " CHAT_ID
[[ -z "$CHAT_ID" ]] && { echo "Ошибка: chat_id пуст"; exit 1; }

cat > "$CONFIG" <<EOF
TELEGRAM_BOT_TOKEN="$TOKEN"
TELEGRAM_CHAT_ID="$CHAT_ID"
EOF
chmod 600 "$CONFIG"

echo ""
echo "Отправляю тестовое сообщение..."
if "$NOTIFY" "✅ <b>AmneziaWG Panel</b>
Тестовое сообщение.
Хост: $(hostname)
Время: $(date '+%F %T')"; then
    echo "✅ Успешно! Проверьте Telegram."
else
    echo "❌ Не удалось отправить. Проверьте токен/chat_id."
    exit 1
fi