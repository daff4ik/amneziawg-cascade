#!/bin/bash
# =====================================================================
#  amnezia-add-client.sh
#  Создаёт нового клиента AmneziaWG, генерирует .conf и vpn:// ссылку.
#  Плейсхолдеры __SERVER_ENDPOINT__, __SERVER_NAME__, __CLIENT_MTU__,
#  __DNS_SERVERS__ подставляет deploy.sh.
# =====================================================================
set -euo pipefail

AWG_DIR="/etc/amnezia/amneziawg"
AWG_IFACE="awg1"
SERVER_ENDPOINT="__SERVER_ENDPOINT__"
SERVER_NAME="__SERVER_NAME__"
CLIENT_MTU="__CLIENT_MTU__"
DNS_SERVERS="__DNS_SERVERS__"
SERVER_PUBKEY_FILE="$AWG_DIR/awg1_public.key"
CLIENTS_DIR="/root/clients"
SUBNET_PREFIX="10.8.0"

# === Проверка прав ===
[[ $EUID -ne 0 ]] && { echo "Ошибка: запускать от root (sudo $0)"; exit 1; }
[[ ! -f "$SERVER_PUBKEY_FILE" ]] && { echo "Ошибка: нет файла $SERVER_PUBKEY_FILE"; exit 1; }

# === Имя клиента ===
CLIENT_NAME=""
if [[ $# -ge 1 ]]; then
    CLIENT_NAME="$1"
else
    echo "=============================================="
    echo " Создание нового клиента AmneziaWG"
    echo "=============================================="
    read -p "Введите имя клиента (буквы, цифры, _, -): " CLIENT_NAME
fi

CLIENT_SAFE=$(echo "$CLIENT_NAME" | sed 's/[^a-zA-Z0-9_-]//g')
[[ -z "$CLIENT_SAFE" ]] && { echo "Ошибка: имя должно содержать только латиницу, цифры, _ или -"; exit 1; }

mkdir -p "$CLIENTS_DIR" && chmod 700 "$CLIENTS_DIR"

# === Проверка существования ===
if [[ -f "$CLIENTS_DIR/${CLIENT_SAFE}.conf" ]] || grep -q "# Client: $CLIENT_SAFE" "$AWG_DIR/$AWG_IFACE.conf"; then
    echo "Ошибка: клиент '$CLIENT_SAFE' уже существует!"
    exit 1
fi

# === Поиск свободного IP ===
USED_IPS=$(grep -oP 'AllowedIPs\s*=\s*\K10\.8\.0\.[0-9]+' "$AWG_DIR/$AWG_IFACE.conf" | awk -F. '{print $4}' || true)
CLIENT_IP_LAST=""
for i in $(seq 2 254); do
    echo "$USED_IPS" | grep -qx "$i" || { CLIENT_IP_LAST=$i; break; }
done
[[ -z "$CLIENT_IP_LAST" ]] && { echo "Ошибка: нет свободных IP в $SUBNET_PREFIX.0/24"; exit 1; }
CLIENT_IP="${SUBNET_PREFIX}.${CLIENT_IP_LAST}"

# === Генерация ключей клиента ===
CLIENT_PRIV=$(awg genkey)
CLIENT_PUB=$(echo "$CLIENT_PRIV" | awg pubkey)

# === Уникальный I1 (маскировочный пакет под DNS) ===
I1=$(python3 - <<'PYEOF'
import os, random
domains = ["google.com","cloudflare.com","yandex.ru","mail.ru","apple.com",
           "icloud.com","amazon.com","microsoft.com","github.com","wikipedia.org",
           "bing.com","duckduckgo.com","mozilla.org","debian.org","ubuntu.com"]
d = random.choice(domains)
labels = d.split(".")
h  = "8580" + "0001" + "0001" + "0000" + "0000"
for lbl in labels:
    h += f"{len(lbl):02x}" + lbl.encode().hex()
h += "00" + "0001" + "0001"
h += "c00c" + "0001" + "0001" + "0000105a" + "0004" + os.urandom(4).hex()
print(f"<r 2><b 0x{h}>")
PYEOF
)

# === Параметры обфускации берём из awg1.conf ===
PARAMS=$(awk '/^\[Interface\]/{f=1;next} /^\[/{f=0} f' "$AWG_DIR/$AWG_IFACE.conf" \
  | grep -E '^(Jc|Jmin|Jmax|S1|S2|S3|S4|H1|H2|H3|H4|HeaderProtectionKey|ContentPaddingAddition|RekeyAfterTime|RekeyTimeout|RejectAfterTime|KeepaliveTimeout|MaxHandshakeAttempts|RandomTrailers|DisableCookies)\s*=' \
  | sed 's/^[[:space:]]*//')

# === Файл клиента ===
CONF="$CLIENTS_DIR/${CLIENT_SAFE}.conf"
cat > "$CONF" <<EOF
[Interface]
PrivateKey = $CLIENT_PRIV
Address = ${CLIENT_IP}/32
MTU = $CLIENT_MTU
DNS = $DNS_SERVERS
$PARAMS
I1 = $I1

[Peer]
PublicKey = $(cat "$SERVER_PUBKEY_FILE")
Endpoint = $SERVER_ENDPOINT
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
EOF
chmod 600 "$CONF"

# === Добавление peer на сервере ===
cat >> "$AWG_DIR/$AWG_IFACE.conf" <<EOF

[Peer]
# Client: $CLIENT_SAFE
PublicKey = $CLIENT_PUB
AllowedIPs = ${CLIENT_IP}/32
EOF

# === Перезапуск ===
systemctl restart "awg-quick@$AWG_IFACE"

echo ""
echo "=============================================="
echo " Клиент '$CLIENT_SAFE' успешно создан"
echo " IP: $CLIENT_IP"
echo " Конфиг: $CONF"
echo "=============================================="
cat "$CONF"

if command -v qrencode >/dev/null 2>&1; then
    echo ""
    echo " QR-код (.conf):"
    qrencode -t ansiutf8 < "$CONF"
fi

# =======================================================
#  Конвертация в vpn:// для AmneziaVPN
# =======================================================
echo ""
read -p "Сконвертировать в vpn:// ссылку Amnezia? (y/n): " CONVERT_ANSWER
case "$CONVERT_ANSWER" in
    [yY]|[yY][eE][sS]|[дД]|[дД][aA])
        echo ""
        echo "Генерация vpn:// ссылки..."

        VPN_LINK=$(python3 - "$CONF" "$SERVER_NAME" <<'PYEOF'
import sys, json, zlib, base64

conf_path = sys.argv[1]
server_name = sys.argv[2]

with open(conf_path, 'r', encoding='utf-8') as f:
    content = f.read()

interface_params = {}
peer_params = {}
current_section = None

for line in content.splitlines():
    line = line.strip()
    if not line or line.startswith('#') or line.startswith(';'):
        continue
    if line.startswith('[') and line.endswith(']'):
        current_section = line[1:-1].strip().lower()
        continue
    if '=' in line:
        k, v = line.split('=', 1)
        k, v = k.strip(), v.strip()
        if current_section == 'interface':
            interface_params[k] = v
        elif current_section == 'peer':
            peer_params[k] = v

endpoint = peer_params.get('Endpoint', '')
if ':' in endpoint:
    host_name, port_str = endpoint.rsplit(':', 1)
    port = int(port_str)
else:
    host_name = endpoint
    port = 51820

dns_str = interface_params.get('DNS', '1.1.1.1, 1.0.0.1')
dns_parts = [d.strip() for d in dns_str.split(',')]
dns1 = dns_parts[0] if len(dns_parts) > 0 else '1.1.1.1'
dns2 = dns_parts[1] if len(dns_parts) > 1 else '1.0.0.1'

client_ip_raw = interface_params.get('Address', '').split('/')[0]

awg_keys = [
    "DisableCookies", "H1", "H2", "H3", "H4", "HeaderProtectionKey", "I1", "I2", "I3", "I4", "I5",
    "Jc", "Jmax", "Jmin", "KeepaliveTimeout", "MaxHandshakeAttempts", "RandomTrailers",
    "RejectAfterTime", "RekeyAfterTime", "RekeyTimeout", "S1", "S2", "S3", "S4"
]

awg_params = {}
for k in awg_keys:
    if k in interface_params:
        awg_params[k] = interface_params[k]

last_config_obj = dict(awg_params)
last_config_obj.update({
    "allowed_ips": [ip.strip() for ip in peer_params.get('AllowedIPs', '0.0.0.0/0').split(',')],
    "client_ip": client_ip_raw,
    "client_priv_key": interface_params.get('PrivateKey', ''),
    "client_pub_key": "",
    "config": content,
    "hostName": host_name,
    "port": port,
    "server_pub_key": peer_params.get('PublicKey', '')
})

awg_container = dict(awg_params)
awg_container.update({
    "last_config": json.dumps(last_config_obj, ensure_ascii=False),
    "port": str(port),
    "protocol_version": "3.1",
    "subnet_address": "10.8.0.0",
    "transport_proto": "udp"
})

full_json = {
    "containers": [
        {
            "awg": awg_container,
            "container": "amnezia-awg2"
        }
    ],
    "defaultContainer": "amnezia-awg2",
    "description": server_name,
    "dns1": dns1,
    "dns2": dns2,
    "format_version": 1,
    "hostName": host_name
}

json_bytes = json.dumps(full_json, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
header = len(json_bytes).to_bytes(4, byteorder='big')
compressed = zlib.compress(json_bytes, level=9)
payload = header + compressed
b64 = base64.urlsafe_b64encode(payload).decode('utf-8')

print(f"vpn://{b64}")
PYEOF
)

        VPN_FILE="$CLIENTS_DIR/${CLIENT_SAFE}.vpn.txt"
        echo "$VPN_LINK" > "$VPN_FILE"
        chmod 600 "$VPN_FILE"

        echo ""
        echo "=============================================="
        echo " Amnezia VPN Link ($CLIENT_SAFE):"
        echo " Имя сервера: $SERVER_NAME"
        echo " Сохранено в файл: $VPN_FILE"
        echo "=============================================="
        echo "$VPN_LINK"
        echo "=============================================="

        if command -v qrencode >/dev/null 2>&1; then
            echo ""
            echo " QR-код для Amnezia (vpn://):"
            qrencode -t ansiutf8 "$VPN_LINK"
        fi
        ;;
    *)
        echo "Конвертация пропущена."
        ;;
esac