#!/bin/bash
# =====================================================================
#  AmneziaWG Cascade Deploy
#  Запускать на SERVER-A (входном) от root.
#
#  Использование:
#    ./deploy.sh              # полная установка
#    ./deploy.sh --update     # обновить скрипты/панель без трогания конфигов и SERVER-B
# =====================================================================
set -euo pipefail

# ==================== Парсинг аргументов ====================
UPDATE_MODE=0
for arg in "$@"; do
    case "$arg" in
        --update) UPDATE_MODE=1 ;;
        --help|-h)
            echo "Использование:"
            echo "  $0              # полная установка"
            echo "  $0 --update     # обновить скрипты и панель (только SERVER-A)"
            exit 0
            ;;
    esac
done

# ==================== Константы ====================
REPO_URL="https://raw.githubusercontent.com/daff4ik/amneziawg-cascade/main"

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
say()  { echo -e "\n\033[1;36m=== $* ===\033[0m"; }
ok()   { echo -e "\033[1;32m[OK]\033[0m $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m $*"; }
err()  { echo -e "\033[1;31m[ERR]\033[0m $*" >&2; }

# =====================================================================
#  Интерактивный ввод
# =====================================================================
say "Ввод параметров"

if [[ $UPDATE_MODE -eq 1 ]]; then
    if [[ ! -f "$AWG_DIR/awg0.conf" ]]; then
        err "Нет $AWG_DIR/awg0.conf — нечего обновлять. Запустите полную установку."
        exit 1
    fi
    say "Режим обновления: SERVER-B не затрагивается"
    
    # Определяем IP этого сервера
    RU_IP=$(hostname -I | awk '{print $1}')
    [[ -z "$RU_IP" ]] && { err "Не удалось определить IP этого сервера"; exit 1; }

    # Имя сервера из существующей панели
    SERVER_NAME=""
    if [[ -f /opt/amnezia-panel.py ]]; then
        SERVER_NAME=$(grep -oP 'SERVER_LABEL\s*=\s*"\K[^"]+' /opt/amnezia-panel.py | head -1 || echo "")
    fi
    
    if [[ -z "$SERVER_NAME" || "$SERVER_NAME" == "__SERVER_LABEL__" || "$SERVER_NAME" == "Ваш лейбл" ]]; then
        read -p "Название сервера для AmneziaVPN (например, 'RuGer1'): " SERVER_NAME
        SERVER_NAME="${SERVER_NAME:-AmneziaVPN}"
    fi

    if [[ -f /etc/amnezia-panel.conf ]]; then
        read -p "Веб-панель уже установлена. Обновить её? (y/n) [y]: " INSTALL_PANEL
        INSTALL_PANEL="${INSTALL_PANEL:-y}"
    else
        read -p "Установить веб-панель? (y/n) [y]: " INSTALL_PANEL
        INSTALL_PANEL="${INSTALL_PANEL:-y}"
    fi

    ok "Параметры для обновления получены"
else
    # --- Полная установка: запрашиваем всё ---
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
fi

# =====================================================================
#  Проверки окружения
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
#  Скачивание helper-скриптов
# =====================================================================
say "Скачивание helper-скриптов из репозитория"

SCRIPTS_TO_DOWNLOAD=(
    amnezia-add-client.sh
    amnezia-remove-client.sh
    amnezia-list-clients.sh
    amnezia-notify.sh
    amnezia-notify-setup.sh
    amnezia-healthcheck.sh
    amnezia-traffic-collector.py
    amnezia-panel-access.sh
)

# Массив зеркал для обхода блокировки GitHub Raw в РФ
MIRRORS=(
    "https://raw.githubusercontent.com/daff4ik/amneziawg-cascade/main"
    "https://ghproxy.net/https://raw.githubusercontent.com/daff4ik/amneziawg-cascade/main"
    "https://mirror.ghproxy.com/https://raw.githubusercontent.com/daff4ik/amneziawg-cascade/main"
    "https://raw.fastgit.org/daff4ik/amneziawg-cascade/main"
)

download_file() {
    local filename=$1
    local dest_path=$2
    local success=0
    
    echo "  ↓ $filename"
    for mirror in "${MIRRORS[@]}"; do
        local url="$mirror/scripts/$filename"
        if curl -fsSL --connect-timeout 5 --max-time 15 "$url" -o "$dest_path"; then
            success=1
            break
        fi
    done
    
    if [[ $success -eq 0 ]]; then
        warn "Не удалось скачать $filename ни с одного зеркала — пропускаю"
        rm -f "$dest_path"
    fi
}

for f in "${SCRIPTS_TO_DOWNLOAD[@]}"; do
    download_file "$f" "/tmp/$f"
    [[ -f "/tmp/$f" ]] && chmod +x "/tmp/$f"
done
ok "Скрипты скачаны"

# =====================================================================
#  Установка AmneziaWG локально (всегда)
# =====================================================================
say "Установка AmneziaWG на SERVER-A"
export DEBIAN_FRONTEND=noninteractive
systemctl stop unattended-upgrades apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl disable unattended-upgrades apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
apt update -qq
apt install -y -qq software-properties-common python3-launchpadlib gnupg2 \
    linux-headers-$(uname -r) build-essential dkms iptables qrencode sqlite3
if ! command -v awg >/dev/null; then
    add-apt-repository -y ppa:amnezia/ppa
    apt update -qq
    apt install -y -qq amneziawg
fi
modprobe amneziawg || true
echo "amneziawg" > /etc/modules-load.d/amneziawg.conf
echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-amneziawg.conf
sysctl --system >/dev/null 2>&1 || true
ok "AmneziaWG на SERVER-A готов"

# =====================================================================
#  Настройка туннеля (ТОЛЬКО при полной установке)
# =====================================================================
if [[ $UPDATE_MODE -eq 0 ]]; then
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

    say "Установка AmneziaWG на SERVER-B ($DE_IP)"
    run_de "bash -s" <<'REMOTE_EOF'
export DEBIAN_FRONTEND=noninteractive
systemctl stop unattended-upgrades apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl disable unattended-upgrades apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
apt update -qq
apt install -y -qq software-properties-common python3-launchpadlib gnupg2 \
    linux-headers-$(uname -r) build-essential dkms iptables qrencode sqlite3
if ! command -v awg >/dev/null; then
    add-apt-repository -y ppa:amnezia/ppa
    apt update -qq
    apt install -y -qq amneziawg
fi
modprobe amneziawg || true
echo "amneziawg" > /etc/modules-load.d/amneziawg.conf
echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-amneziawg.conf
sysctl --system >/dev/null 2>&1 || true
echo INSTALLED
REMOTE_EOF
    ok "AmneziaWG на SERVER-B готов"

    # ---------- Генерация параметров ----------
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

    HEADER_PROTECTION_KEY=$(awg genkey)
    Jc=$((RANDOM % 6 + 3))
    JMIN=$((RANDOM % 40 + 10))
    JMAX=$((JMIN + RANDOM % 40 + 20))
    read S1 S2 <<< "$(gen_s_pair)"
    S3=$((RANDOM % 50 + 12))
    S4=$((RANDOM % 50 + 12))
    read H1 H2 H3 H4 <<< "$(gen_ranges)"

    HEADER_PROTECTION_KEY_C=$(awg genkey)
    Jc_C=$((RANDOM % 6 + 3))
    JMIN_C=$((RANDOM % 40 + 10))
    JMAX_C=$((JMIN_C + RANDOM % 40 + 20))
    read S1_C S2_C <<< "$(gen_s_pair)"
    S3_C=$((RANDOM % 50 + 12))
    S4_C=$((RANDOM % 50 + 12))
    read H1_C H2_C H3_C H4_C <<< "$(gen_ranges)"
    ok "Параметры сгенерированы"

    # ---------- Ключи awg0 ----------
    say "Генерация ключей"
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

    DE_IFACE=$(run_de "ip route get 8.8.8.8 | awk '{print \$5}' | head -1")
    [[ -z "$DE_IFACE" ]] && { err "Не удалось определить внешний интерфейс SERVER-B"; exit 1; }
    ok "Внешний интерфейс SERVER-B: $DE_IFACE"

    # ---------- Конфиги awg0 ----------
    say "Настройка туннеля awg0"
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

    # ---------- Запуск awg0 ----------
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
        exit 1
    fi

    # ---------- Клиентский интерфейс awg1 ----------
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
    ok "awg1 поднят"
fi  # <-- КОНЕЦ блока "полная установка"

# =====================================================================
#  Установка helper-скриптов (всегда)
# =====================================================================
say "Установка helper-скриптов"
mkdir -p /root/clients && chmod 700 /root/clients
mkdir -p /usr/local/bin

for script in "${SCRIPTS_TO_DOWNLOAD[@]}"; do
    if [[ -f "/tmp/$script" ]]; then
        sed -e "s|__SERVER_ENDPOINT__|$RU_IP:$AWG_CLIENT_PORT|g" \
            -e "s|__SERVER_NAME__|$SERVER_NAME|g" \
            -e "s|__CLIENT_MTU__|$CLIENT_MTU|g" \
            -e "s|__DNS_SERVERS__|$CLIENT_DNS|g" \
            "/tmp/$script" > "/usr/local/bin/$script"
        chmod +x "/usr/local/bin/$script"
    fi
done
ok "Скрипты установлены в /usr/local/bin/"

# =====================================================================
#  Установка cron
# =====================================================================
say "Настройка cron"
apt install -y -qq sqlite3 >/dev/null 2>&1 || true
mkdir -p /var/lib/amnezia-panel
[[ -f /var/lib/amnezia-panel/traffic_enabled ]] || echo "1" > /var/lib/amnezia-panel/traffic_enabled
rm -f /usr/local/bin/amnezia-traffic-collector.sh

if [[ -x /usr/local/bin/amnezia-healthcheck.sh ]]; then
    cat > /etc/cron.d/amnezia-healthcheck <<'EOF'
0 * * * * root /usr/local/bin/amnezia-healthcheck.sh
EOF
    chmod 644 /etc/cron.d/amnezia-healthcheck
    ok "cron: healthcheck (каждый час)"
fi

if [[ -x /usr/local/bin/amnezia-traffic-collector.py ]]; then
    cat > /etc/cron.d/amnezia-traffic <<'EOF'
*/5 * * * * root /usr/local/bin/amnezia-traffic-collector.py
EOF
    chmod 644 /etc/cron.d/amnezia-traffic
    /usr/local/bin/amnezia-traffic-collector.py 2>/dev/null || true
    ok "cron: traffic collector (каждые 5 минут)"
fi

# =====================================================================
#  Первый клиент admin (только при полной установке)
# =====================================================================
if [[ $UPDATE_MODE -eq 0 ]]; then
    say "Создание первого клиента (admin)"
    if [[ ! -f /root/clients/admin.conf ]] && [[ -x /usr/local/bin/amnezia-add-client.sh ]]; then
        echo "n" | /usr/local/bin/amnezia-add-client.sh admin || warn "admin уже существует или создан"
    else
        ok "Клиент admin уже существует"
    fi
fi

# =====================================================================
#  Веб-панель
# =====================================================================
if [[ "$INSTALL_PANEL" =~ ^[yYдД] ]]; then
    say "Установка веб-панели"

    if [[ $UPDATE_MODE -eq 1 && -f /etc/amnezia-panel.conf ]]; then
        PANEL_USER=$(grep -oP '^username=\K.*' /etc/amnezia-panel.conf || echo admin)
        PANEL_PASS=$(grep -oP '^password=\K.*' /etc/amnezia-panel.conf || echo "")
        AUTH_CHOICE="keep"
        ok "Учётные данные панели сохраняются"
    else
        echo ""
        echo "Выберите способ создания учётных данных для панели:"
        echo "  1) Сгенерировать автоматически (рекомендуется)"
        echo "  2) Указать свой логин и пароль"
        echo ""
        read -p "Ваш выбор [1]: " AUTH_CHOICE
        AUTH_CHOICE="${AUTH_CHOICE:-1}"

        case "$AUTH_CHOICE" in
            2)
                read -p "Логин: " PANEL_USER
                [[ -z "$PANEL_USER" ]] && { err "Логин не может быть пустым"; exit 1; }
                while :; do
                    read -r -s -p "Пароль (мин. 8 символов): " PANEL_PASS; echo
                    [[ ${#PANEL_PASS} -lt 8 ]] && { warn "Пароль слишком короткий, попробуйте снова"; continue; }
                    read -r -s -p "Повторите пароль: " PANEL_PASS2; echo
                    [[ "$PANEL_PASS" != "$PANEL_PASS2" ]] && { warn "Пароли не совпадают, попробуйте снова"; continue; }
                    break
                done
                ok "Учётные данные приняты"
                ;;
            *)
                PANEL_USER="admin"
                PANEL_PASS=$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)
                ok "Учётные данные сгенерированы автоматически"
                ;;
        esac
    fi

   echo "  ↓ Скачивание файлов веб-панели"
    PANEL_DL_OK=0
    for mirror in "${MIRRORS[@]}"; do
        if curl -fsSL --connect-timeout 5 --max-time 15 "$mirror/web-panel/amnezia-panel.py" -o /tmp/amnezia-panel.py && \
           curl -fsSL --connect-timeout 5 --max-time 15 "$mirror/web-panel/amnezia-panel.service" -o /tmp/amnezia-panel.service; then
            PANEL_DL_OK=1
            break
        fi
    done
    
    if [[ $PANEL_DL_OK -eq 0 ]]; then
        err "Не удалось скачать файлы панели ни с одного зеркала"
        exit 1
    fi

    apt install -y -qq python3-venv
    python3 -m venv /opt/amnezia-panel-venv
    /opt/amnezia-panel-venv/bin/pip install --quiet --upgrade pip
    /opt/amnezia-panel-venv/bin/pip install --quiet fastapi 'uvicorn[standard]' python-multipart qrcode

    sed "s|__SERVER_LABEL__|$SERVER_NAME|g" /tmp/amnezia-panel.py > /opt/amnezia-panel.py
    chmod 700 /opt/amnezia-panel.py

    if [[ "$AUTH_CHOICE" != "keep" ]]; then
        cat > /etc/amnezia-panel.conf <<EOF
username=$PANEL_USER
password=$PANEL_PASS
EOF
        chmod 600 /etc/amnezia-panel.conf
    fi

    mkdir -p /etc/amnezia-panel-ssl
    if [[ ! -f /etc/amnezia-panel-ssl/cert.pem ]]; then
        openssl req -x509 -newkey rsa:4096 \
            -keyout /etc/amnezia-panel-ssl/key.pem \
            -out /etc/amnezia-panel-ssl/cert.pem \
            -days 3650 -nodes -subj "/CN=amnezia-panel" 2>/dev/null
        chmod 600 /etc/amnezia-panel-ssl/*.pem
    fi

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
        if [[ "$AUTH_CHOICE" == "2" ]]; then
            echo "  Пароль:   (задан вами при установке)"
        elif [[ "$AUTH_CHOICE" == "keep" ]]; then
            echo "  Пароль:   (не изменён, см. /etc/amnezia-panel.conf)"
        else
            echo "  Пароль:   $PANEL_PASS"
            echo ""
            echo "  СОХРАНИТЕ ПАРОЛЬ! Больше он не будет показан."
        fi
        echo "=============================================="
    else
        err "Веб-панель не запустилась"
        err "Проверьте: sudo journalctl -u amnezia-panel -n 30"
    fi

    rm -f /tmp/amnezia-panel.py /tmp/amnezia-panel.service
fi

# =====================================================================
#  Сохранение текущего коммита
# =====================================================================
if [[ "$INSTALL_PANEL" =~ ^[yYдД] ]]; then
    echo "  ↓ Сохранение текущей версии репозитория"
    LATEST_SHA=$(curl -s "https://api.github.com/repos/daff4ik/amneziawg-cascade/commits/main" | grep -oP '"sha":\s*"\K[^"]+' | head -1)
    if [[ -n "$LATEST_SHA" ]]; then
        mkdir -p /var/lib/amnezia-panel
        echo "$LATEST_SHA" > /var/lib/amnezia-panel/installed_commit
        ok "Версия сохранена: $LATEST_SHA"
    else
        warn "Не удалось получить SHA с GitHub"
    fi
fi

say "ГОТОВО"
if [[ $UPDATE_MODE -eq 1 ]]; then
    echo "Режим обновления завершён."
    echo "Конфиги awg0/awg1 и клиенты не изменены."
    echo ""
    echo "Обновлены: helper-скрипты, cron, панель (если выбрано)."
else
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
  Настроить Telegram:  sudo amnezia-notify-setup.sh

Конфиги клиентов:      /root/clients/<имя>.conf
vpn:// ссылки:         /root/clients/<имя>.vpn.txt

=== ДИАГНОСТИКА ===

  Статус туннеля:      sudo awg show
  Проверить связь:     ping -c 3 $TUNNEL_B_IP
  Healthcheck вручную: sudo amnezia-healthcheck.sh
  Собрать трафик:      sudo amnezia-traffic-collector.py
  БД трафика:          /var/lib/amnezia-panel/traffic.db

EOM
fi

if [[ "$INSTALL_PANEL" =~ ^[yYдД] ]]; then
    echo "Веб-панель:          https://$RU_IP:8000"
    echo "Учётные данные:      /etc/amnezia-panel.conf"
    echo ""
fi

echo "Приятного использования!"