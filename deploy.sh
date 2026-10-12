#!/bin/bash
# =====================================================================
#  AmneziaWG Cascade Deploy
#  Автоматическое развёртывание каскадного VPN между двумя серверами.
#  Запускать на SERVER-A (входном) от root.
#
#  Перед публикацией на GitHub отредактируйте REPO_URL ниже.
# =====================================================================
set -euo pipefail

# =====================================================================
#  НАСТРОЙКА: замените на URL вашего репозитория
# =====================================================================
REPO_URL="https://raw.githubusercontent.com/davideprikyan/amneziawg-cascade/main"
# Пример: https://raw.githubusercontent.com/username/amneziawg-cascade/main

# ==================== Константы ====================
AWG_DIR="/etc/amnezia/amneziawg"
AWG_TUNNEL_PORT="8443"
AWG_CLIENT_PORT="585"
TUNNEL_A_IP="10.10.10.2"
TUNNEL_B_IP="10.10.10.1"
CLIENT_SUBNET="10.8.0"
CLIENT_SERVER_IP="10.8.0.1"
MTU_AWG0="1180"
MTU_AWG1="1100"
MSS_CLAMP="1000"
CLIENT_MTU="1280"
CLIENT_DNS="1.1.1.1, 1.0.0.1"

# ==================== Хелперы ====================
say() { echo -e "\n\033[1;36m=== $* ===\033[0m"; }
ok()  { echo -e "\033[1;32m[OK]\033[0m $*"; }
warn(){ echo -e "\033[1;33m[WARN]\033[0m $*"; }
err() { echo -e "\033[1;31m[ERR]\033[0m $*" >&2; }

# =====================================================================
#  Проверка REPO_URL
# =====================================================================
if [[ "$REPO_URL" == *"CHANGE_ME"* ]]; then
    err "Отредактируйте REPO_URL в начале deploy.sh!"
    err "Текущее значение: $REPO_URL"
    err "Укажите ваш репозиторий, например:"
    err "  https://raw.githubusercontent.com/username/amneziawg-cascade/main"
    exit 1
fi

# =====================================================================
#  Интерактивный ввод
# =====================================================================
say "Ввод параметров"

read -p "IP-адрес этого сервера (SERVER-A): " RU_IP
[[ -z "$RU_IP" ]] && { err "IP обязателен"; exit 1; }

read -p "IP-адрес второго сервера (SERVER-B): " DE_IP
[[ -z "$DE_IP" ]] && { err "IP обязателен"; exit 1; }

read -p "SSH-порт SERVER-B [22]: " DE_SSH_PORT
DE_SSH_PORT="${DE_SSH_PORT:-22}"

read -p "SSH-пользователь SERVER-B [root]: " DE_SSH_USER
DE_SSH_USER="${DE_SSH_USER:-root}"

read -r -s -p "SSH-пароль SERVER-B: " DE_PASS; echo
[[ -z "$DE_PASS" ]] && { err "Пароль обязателен"; exit 1; }

read -p "Название сервера для AmneziaVPN (например, 'MyVPN'): " SERVER_NAME
SERVER_NAME="${SERVER_NAME:-AmneziaVPN}"

read -p "Установить веб-панель? (y/n) [n]: " INSTALL_PANEL
INSTALL_PANEL="${INSTALL_PANEL:-n}"

# =====================================================================
#  Проверки
# =====================================================================
say "Проверка окружения"
[[ $EUID -ne 0 ]] && { err "Запускать от root"; exit 1; }
command -v apt >/dev/null || { err "Нужен Ubuntu/Debian"; exit 1; }
command -v curl >/dev/null || { apt update -qq && apt install -y -qq curl; }
command -v python3 >/dev/null || { err "Нужен python3"; exit 1; }

HOST_IPS=$(hostname -I || true)
if [[ "$HOST_IPS" != *"$RU_IP"* ]]; then
    warn "IP $RU_IP не найден среди адресов этого сервера ($HOST_IPS)"
    read -p "Продолжить? (y/n): " ans
    [[ "$ans" =~ ^[yYдД] ]] || exit 1
fi

# =====================================================================
#  Скачивание helper-скриптов из репозитория
# =====================================================================
say "Скачивание helper-скриптов из репозитория"
for f in amnezia-add-client.sh amnezia-remove-client.sh amnezia-list-clients.sh; do
    url="$REPO_URL/scripts/$f"
    echo "  ↓ $f"
    if ! curl -fsSL "$url" -o "/tmp/$f"; then
        err "Не удалось скачать $url"
        err "Проверьте REPO_URL и доступность репозитория."
        exit 1
    fi
    chmod +x "/tmp/$f"
done
ok "Все скрипты скачаны"

# =====================================================================
#  SSH к SERVER-B
# =====================================================================
say "Установка sshpass и проверка SSH"
command -v sshpass >/dev/null || { apt update -qq && apt install -y -qq sshpass; }
export SSHPASS="$DE_PASS"

run_de() {
    sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=15 \
        -p "$DE_SSH_PORT" "$DE_SSH_USER@$DE_IP" "$@"
}
scp_de() {
    sshpass -e scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -P "$DE_SSH_PORT" "$1" "$DE_SSH_USER@$DE_IP:$2"
}

run_de "echo SSH_OK" | grep -q SSH_OK || { err "SSH до $DE_IP не работает"; exit 1; }
ok "SSH работает"

# =====================================================================
#  Установка AmneziaWG на оба сервера
# =====================================================================
install_awg() {
    local target="$1"
    local cmd
    cmd=$(cat <<'REMOTE_EOF'
export DEBIAN_FRONTEND=noninteractive
systemctl stop unattended-upgrades apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl disable unattended-upgrades apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
apt update -qq
apt install -y -qq software-properties-common python3-launchpadlib gnupg2 \
    linux-headers-$(uname -r) build-essential dkms iptables qrencode
if ! command -v awg >/dev/null; then
    add-apt-repository -y ppa:amnezia/ppa
    apt update -qq
    apt install -y -qq amneziawg
fi
modprobe amneziawg || true
echo "amneziawg" > /etc/modules-load.d/amneziawg.conf
echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-amneziawg.conf
sysctl --system >/dev/null
echo INSTALLED
REMOTE_EOF
    )
    if [[ "$target" == "local" ]]; then
        bash -c "$cmd"
    else
        run_de "bash -s" <<< "$cmd"
    fi
}

say "Установка AmneziaWG на SERVER-A (этот сервер)"
install_awg local

say "Установка AmneziaWG на SERVER-B ($DE_IP)"
install_awg remote

# =====================================================================
#  Генерация параметров обфускации (уникальные, генерируются заново)
# =====================================================================
say "Генерация параметров обфускации"

gen_ranges() {
    python3 - <<'PY'
import random
ranges = []
while len(ranges) < 4:
    start = random.randint(5, 2_000_000_000)
    end = start + random.randint(10_000_000, 100_000_000)
    if end > 2_147_483_647: continue
    if all(end < r[0] or start > r[1] for r in ranges):
        ranges.append((start, end))
print(" ".join(f"{s}-{e}" for s, e in ranges))
PY
}

gen_s_pair() {
    while :; do
        local a=$((RANDOM % 50 + 12))
        local b=$((RANDOM % 50 + 12))
        [[ $((a + 56)) -ne $b ]] && { echo "$a $b"; return; }
    done
}

# Параметры для awg0
HEADER_PROTECTION_KEY=$(awg genkey)
Jc=$((RANDOM % 6 + 3))
JMIN=$((RANDOM % 40 + 10))
JMAX=$((JMIN + RANDOM % 40 + 20))
read S1 S2 <<< "$(gen_s_pair)"
S3=$((RANDOM % 50 + 12))
S4=$((RANDOM % 50 + 12))
read H1 H2 H3 H4 <<< "$(gen_ranges)"

# Параметры для awg1 (клиентские, независимые)
HEADER_PROTECTION_KEY_C=$(awg genkey)
Jc_C=$((RANDOM % 6 + 3))
JMIN_C=$((RANDOM % 40 + 10))
JMAX_C=$((JMIN_C + RANDOM % 40 + 20))
read S1_C S2_C <<< "$(gen_s_pair)"
S3_C=$((RANDOM % 50 + 12))
S4_C=$((RANDOM % 50 + 12))
read H1_C H2_C H3_C H4_C <<< "$(gen_ranges)"

ok "Параметры сгенерированы"

# =====================================================================
#  Генерация ключей awg0 на двух серверах
# =====================================================================
say "Генерация ключей межсерверного туннеля"

mkdir -p "$AWG_DIR" && chmod 700 "$AWG_DIR"
cd "$AWG_DIR"
if [[ ! -f private.key ]]; then
    umask 077
    awg genkey > private.key
    awg pubkey < private.key > public.key
    chmod 600 private.key
    chmod 644 public.key
fi
RU_PRIV=$(cat private.key)
RU_PUB=$(cat public.key)

run_de "mkdir -p $AWG_DIR && chmod 700 $AWG_DIR && cd $AWG_DIR && \
    if [ ! -f private.key ]; then umask 077; awg genkey > private.key; awg pubkey < private.key > public.key; chmod 600 private.key; chmod 644 public.key; fi"
DE_PRIV=$(run_de "cat $AWG_DIR/private.key")
DE_PUB=$(run_de "cat $AWG_DIR/public.key")

# =====================================================================
#  Определение внешнего интерфейса на SERVER-B
# =====================================================================
DE_IFACE=$(run_de "ip route get 8.8.8.8 | awk '{print \$5}' | head -1")
[[ -z "$DE_IFACE" ]] && { err "Не удалось определить внешний интерфейс SERVER-B"; exit 1; }
ok "Внешний интерфейс SERVER-B: $DE_IFACE"

# =====================================================================
#  Запись awg0.conf
# =====================================================================
say "Настройка туннеля awg0"

# --- SERVER-A ---
cat > "$AWG_DIR/awg0.conf" <<EOF
[Interface]
PrivateKey = $RU_PRIV
Address = $TUNNEL_A_IP/32
ListenPort = $AWG_TUNNEL_PORT
MTU = $MTU_AWG0
Table = off
Jc = $Jc
Jmin = $JMIN
Jmax = $JMAX
S1 = $S1
S2 = $S2
S3 = $S3
S4 = $S4
H1 = $H1
H2 = $H2
H3 = $H3
H4 = $H4
HeaderProtectionKey = $HEADER_PROTECTION_KEY
ContentPaddingAddition = 16
RekeyAfterTime = 100-120
RekeyTimeout = 3-7
RejectAfterTime = 150-180
KeepaliveTimeout = 5-15
MaxHandshakeAttempts = 15-20
RandomTrailers = off
DisableCookies = on
PostUp = ip route add $TUNNEL_B_IP/32 dev awg0
PostUp = ip route add default dev awg0 table 100
PostUp = ip rule add from $CLIENT_SUBNET.0/24 lookup 100
PostUp = iptables -A FORWARD -i awg1 -o awg0 -j ACCEPT
PostUp = iptables -A FORWARD -i awg0 -o awg1 -j ACCEPT
PostUp = iptables -t nat -A POSTROUTING -o awg0 -j MASQUERADE
PostUp = while iptables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss $MSS_CLAMP 2>/dev/null; do :; done
PostUp = iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss $MSS_CLAMP
PostDown = ip rule del from $CLIENT_SUBNET.0/24 lookup 100
PostDown = ip route del default dev awg0 table 100
PostDown = ip route del $TUNNEL_B_IP/32 dev awg0
PostDown = iptables -D FORWARD -i awg1 -o awg0 -j ACCEPT
PostDown = iptables -D FORWARD -i awg0 -o awg1 -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -o awg0 -j MASQUERADE
PostDown = iptables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss $MSS_CLAMP 2>/dev/null || true

[Peer]
PublicKey = $DE_PUB
Endpoint = $DE_IP:$AWG_TUNNEL_PORT
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
EOF
chmod 600 "$AWG_DIR/awg0.conf"

# --- SERVER-B ---
cat > /tmp/awg0-de.conf <<EOF
[Interface]
PrivateKey = $DE_PRIV
Address = $TUNNEL_B_IP/32
ListenPort = $AWG_TUNNEL_PORT
MTU = $MTU_AWG0
Jc = $Jc
Jmin = $JMIN
Jmax = $JMAX
S1 = $S1
S2 = $S2
S3 = $S3
S4 = $S4
H1 = $H1
H2 = $H2
H3 = $H3
H4 = $H4
HeaderProtectionKey = $HEADER_PROTECTION_KEY
ContentPaddingAddition = 16
RekeyAfterTime = 100-120
RekeyTimeout = 3-7
RejectAfterTime = 150-180
KeepaliveTimeout = 5-15
MaxHandshakeAttempts = 15-20
RandomTrailers = off
DisableCookies = on
PostUp = iptables -A FORWARD -i awg0 -j ACCEPT
PostUp = iptables -A FORWARD -o awg0 -j ACCEPT
PostUp = iptables -t nat -A POSTROUTING -o $DE_IFACE -j MASQUERADE
PostUp = while iptables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss $MSS_CLAMP 2>/dev/null; do :; done
PostUp = iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss $MSS_CLAMP
PostDown = iptables -D FORWARD -i awg0 -j ACCEPT
PostDown = iptables -D FORWARD -o awg0 -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -o $DE_IFACE -j MASQUERADE
PostDown = iptables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss $MSS_CLAMP 2>/dev/null || true

[Peer]
PublicKey = $RU_PUB
AllowedIPs = $TUNNEL_A_IP/32
EOF

scp_de /tmp/awg0-de.conf "$AWG_DIR/awg0.conf"
run_de "chmod 600 $AWG_DIR/awg0.conf"
rm -f /tmp/awg0-de.conf
ok "Конфиги awg0 записаны"

# =====================================================================
#  Запуск awg0
# =====================================================================
say "Запуск межсерверного туннеля"
run_de "systemctl enable awg-quick@awg0 && systemctl restart awg-quick@awg0"
sleep 2
systemctl enable awg-quick@awg0
systemctl restart awg-quick@awg0
sleep 3

if ping -c 3 -W 3 "$TUNNEL_B_IP" >/dev/null 2>&1; then
    ok "Туннель awg0 работает"
else
    err "Пинг до $TUNNEL_B_IP не идёт"
    warn "Диагностика:"
    warn "  SERVER-A: sudo journalctl -xeu awg-quick@awg0 -n 30"
    warn "  SERVER-B: sudo journalctl -xeu awg-quick@awg0 -n 30"
    exit 1
fi

# =====================================================================
#  Клиентский интерфейс awg1
# =====================================================================
say "Настройка клиентского интерфейса awg1"

if [[ ! -f "$AWG_DIR/awg1_private.key" ]]; then
    umask 077
    awg genkey > "$AWG_DIR/awg1_private.key"
    awg pubkey < "$AWG_DIR/awg1_private.key" > "$AWG_DIR/awg1_public.key"
    chmod 600 "$AWG_DIR/awg1_private.key"
    chmod 644 "$AWG_DIR/awg1_public.key"
fi
AWG1_PRIV=$(cat "$AWG_DIR/awg1_private.key")

cat > "$AWG_DIR/awg1.conf" <<EOF
[Interface]
PrivateKey = $AWG1_PRIV
Address = $CLIENT_SERVER_IP/24
ListenPort = $AWG_CLIENT_PORT
MTU = $MTU_AWG1
Jc = $Jc_C
Jmin = $JMIN_C
Jmax = $JMAX_C
S1 = $S1_C
S2 = $S2_C
S3 = $S3_C
S4 = $S4_C
H1 = $H1_C
H2 = $H2_C
H3 = $H3_C
H4 = $H4_C
HeaderProtectionKey = $HEADER_PROTECTION_KEY_C
ContentPaddingAddition = 16
RekeyAfterTime = 100-120
RekeyTimeout = 3-7
RejectAfterTime = 150-180
KeepaliveTimeout = 5-15
MaxHandshakeAttempts = 15-20
RandomTrailers = on
DisableCookies = on
EOF
chmod 600 "$AWG_DIR/awg1.conf"

systemctl enable awg-quick@awg1
systemctl restart awg-quick@awg1
sleep 2
ok "awg1 поднят (MTU=$(ip -o link show awg1 2>/dev/null | grep -oP 'mtu \K[0-9]+' || echo '?'))"

# =====================================================================
#  Установка helper-скриптов
# =====================================================================
say "Установка helper-скриптов"
mkdir -p /root/clients && chmod 700 /root/clients
mkdir -p /usr/local/bin

for script in amnezia-add-client.sh amnezia-remove-client.sh amnezia-list-clients.sh; do
    if [[ -f "/tmp/$script" ]]; then
        sed -e "s|__SERVER_ENDPOINT__|$RU_IP:$AWG_CLIENT_PORT|g" \
            -e "s|__SERVER_NAME__|$SERVER_NAME|g" \
            -e "s|__CLIENT_MTU__|$CLIENT_MTU|g" \
            -e "s|__DNS_SERVERS__|$CLIENT_DNS|g" \
            "/tmp/$script" > "/usr/local/bin/$script"
        chmod +x "/usr/local/bin/$script"
    fi
done
ok "Скрипты установлены: amnezia-add-client.sh, amnezia-remove-client.sh, amnezia-list-clients.sh"

# =====================================================================
#  Первый клиент admin
# =====================================================================
say "Создание первого клиента (admin)"
if [[ ! -f /root/clients/admin.conf ]] && [[ -x /usr/local/bin/amnezia-add-client.sh ]]; then
    echo "n" | /usr/local/bin/amnezia-add-client.sh admin || warn "admin уже существует или создан"
    echo ""
    echo "Для генерации vpn:// ссылки для admin выполните:"
    echo "  sudo amnezia-add-client.sh admin"
    echo "  (ответьте 'y' на вопрос про конвертацию)"
else
    ok "Клиент admin уже существует"
fi

# =====================================================================
#  Веб-панель (опционально)
# =====================================================================
if [[ "$INSTALL_PANEL" =~ ^[yYдД] ]]; then
    say "Установка веб-панели"

    echo "  ↓ Скачивание amnezia-panel.py"
    curl -fsSL "$REPO_URL/web-panel/amnezia-panel.py" -o /tmp/amnezia-panel.py || { err "Не удалось скачать amnezia-panel.py"; exit 1; }
    echo "  ↓ Скачивание amnezia-panel.service"
    curl -fsSL "$REPO_URL/web-panel/amnezia-panel.service" -o /tmp/amnezia-panel.service || { err "Не удалось скачать amnezia-panel.service"; exit 1; }

    # Установка Python venv
    apt install -y -qq python3-venv
    python3 -m venv /opt/amnezia-panel-venv
    /opt/amnezia-panel-venv/bin/pip install --quiet --upgrade pip
    /opt/amnezia-panel-venv/bin/pip install --quiet fastapi 'uvicorn[standard]' python-multipart qrcode

    # Подстановка label
    sed "s|__SERVER_LABEL__|$SERVER_NAME|g" /tmp/amnezia-panel.py > /opt/amnezia-panel.py
    chmod 700 /opt/amnezia-panel.py

    # Логин/пароль
    PANEL_USER="admin"
    PANEL_PASS=$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)
    cat > /etc/amnezia-panel.conf <<EOF
username=$PANEL_USER
password=$PANEL_PASS
EOF
    chmod 600 /etc/amnezia-panel.conf

    # Self-signed SSL
    mkdir -p /etc/amnezia-panel-ssl
    if [[ ! -f /etc/amnezia-panel-ssl/cert.pem ]]; then
        openssl req -x509 -newkey rsa:4096 \
            -keyout /etc/amnezia-panel-ssl/key.pem \
            -out /etc/amnezia-panel-ssl/cert.pem \
            -days 3650 -nodes -subj "/CN=amnezia-panel" 2>/dev/null
        chmod 600 /etc/amnezia-panel-ssl/*.pem
    fi

    # Service
    cp /tmp/amnezia-panel.service /etc/systemd/system/amnezia-panel.service
    systemctl daemon-reload
    systemctl enable amnezia-panel
    systemctl restart amnezia-panel
    sleep 2

    if systemctl is-active --quiet amnezia-panel; then
        ok "Веб-панель запущена"
        echo ""
        echo "=============================================="
        echo "  ВЕБ-ПАНЕЛЬ"
        echo "=============================================="
        echo "  URL:      https://$RU_IP:8000"
        echo "  Логин:    $PANEL_USER"
        echo "  Пароль:   $PANEL_PASS"
        echo ""
        echo "  СОХРАНИТЕ ЭТИ ДАННЫЕ! Пароль больше не будет показан."
        echo "  (также записан в /etc/amnezia-panel.conf)"
        echo "=============================================="
    else
        err "Веб-панель не запустилась"
        err "Проверьте: sudo journalctl -u amnezia-panel -n 30"
    fi

    rm -f /tmp/amnezia-panel.py /tmp/amnezia-panel.service
fi

# =====================================================================
#  Итоговая сводка
# =====================================================================
say "ГОТОВО"
cat <<EOM

Каскадный VPN развёрнут.

  SERVER-A (этот): $RU_IP
  SERVER-B:        $DE_IP
  Туннель awg0:    порт $AWG_TUNNEL_PORT/udp
  Клиенты awg1:    порт $AWG_CLIENT_PORT/udp
  Подсеть:         $CLIENT_SUBNET.0/24

=== КОМАНДЫ УПРАВЛЕНИЯ КЛИЕНТАМИ ===

  Добавить клиента:    sudo amnezia-add-client.sh <имя>
  Удалить клиента:     sudo amnezia-remove-client.sh <имя>
  Список клиентов:     sudo amnezia-list-clients.sh

Конфиги клиентов:      /root/clients/<имя>.conf
vpn:// ссылки:         /root/clients/<имя>.vpn.txt

=== ДИАГНОСТИКА ===

  Статус туннеля:      sudo awg show
  Проверить связь:     ping -c 3 $TUNNEL_B_IP
  Логи awg0:           sudo journalctl -xeu awg-quick@awg0 -n 30
  Логи awg1:           sudo journalctl -xeu awg-quick@awg1 -n 30

EOM

if [[ "$INSTALL_PANEL" =~ ^[yYдД] ]]; then
    echo "Веб-панель:          https://$RU_IP:8000"
    echo "Учётные данные:      /etc/amnezia-panel.conf"
    echo ""
fi

echo "Приятного использования!"
