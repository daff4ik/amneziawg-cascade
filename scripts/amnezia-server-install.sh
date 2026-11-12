#!/bin/bash
# =====================================================================
#  amnezia-server-install.sh
#  Базовая установка AmneziaWG-узла (используется для SERVER-A и SERVER-B,
#  а также веб-панелью при переключении выходного сервера).
#
#  Флаги:
#    --ipv6      включить форвардинг IPv6 (для v6-туннеля)
#    --fail2ban  установить и настроить Fail2Ban (защита SSH)
# =====================================================================
set -euo pipefail

WITH_IPV6=0
WITH_F2B=0
for arg in "$@"; do
    case "$arg" in
        --ipv6)     WITH_IPV6=1 ;;
        --fail2ban) WITH_F2B=1 ;;
    esac
done

export DEBIAN_FRONTEND=noninteractive

# Приглушаем unattended-upgrades на время установки (во избежание блокировки apt)
systemctl stop unattended-upgrades apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true

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

if [[ $WITH_IPV6 -eq 1 ]]; then
    cat > /etc/sysctl.d/99-amneziawg.conf <<'EOF'
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
EOF
else
    echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-amneziawg.conf
fi
sysctl --system >/dev/null 2>&1 || true

if [[ $WITH_F2B -eq 1 ]]; then
    apt install -y -qq fail2ban
    cat > /etc/fail2ban/jail.local <<'EOF'
[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
maxretry = 5
bantime = 3600
findtime = 600
EOF
    systemctl enable fail2ban
    systemctl restart fail2ban
fi

# Возвращаем unattended-upgrades
systemctl enable unattended-upgrades apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl start apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true

echo INSTALLED_OK
