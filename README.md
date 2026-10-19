# 🚀 Свой VPN с нуля — пошаговое руководство для новичков

> Если вы никогда не арендовали сервер, не работали с командной строкой и не знаете, что такое SSH — **начните с этого раздела**. Здесь без технического жаргона объясняется, что нужно, зачем и как.

## 💻 Что такое SSH и где взять данные для подключения

**SSH** (Secure Shell, «безопасная оболочка») — это способ управлять сервером удалённо через интернет. Представьте, что вы садитесь за компьютер, который физически стоит в дата-центре в другой стране, но видите его экран и можете вводить команды у себя дома.

SSH — стандарт де-факто для управления любыми серверами Linux. Через него работают системные администраторы всего мира.

---

### 📧 Откуда взять данные для подключения

**Сразу после оплаты VPS** провайдер присылает на вашу электронную почту **приветственное письмо** с данными для входа. Обычно это выглядит так:

---

## 🤔 Что такое «свой VPN» и зачем он нужен

Обычный VPN, который вы покупаете за 5 $/месяц (NordVPN, ExpressVPN и т.п.) — это чужая инфраструктура. Вы не знаете, кто владеет серверами, какие логи они ведут, и не можете гарантировать, что там нет «дыр» для спецслужб.

**Свой VPN** (или **self-hosted**) — это VPN, где **сервер принадлежит только вам**. Вы его арендуете у хостинг-провайдера, сами устанавливаете софт, сами управляете. Никто, кроме вас, не имеет доступа к серверу.

**Плюсы:**
- 🔒 Никаких логов — вы сами решаете, что хранить
- 🚀 Никто не разделяет с вами канал — скорость только ваша
- 🎛️ Вы решаете, какие протоколы использовать и как настраивать
- 🌍 Можно поставить сервер в конкретной стране (например, в Германии)

**Минусы:**
- 💰 Стоит ~3–5 $/месяц за сервер (но это в 2 раза дешевле коммерческого VPN)
- 🛠️ Нужно один раз настроить (этот проект автоматизирует процесс)
- 📚 Нужно потратить 20–30 минут на чтение этой инструкции

---

## ❓ Что такое VPS и зачем он нужен

**VPS** (Virtual Private Server) — это виртуальный сервер, который вы арендуете у хостинг-провайдера. Выглядит как обычный компьютер, но находится в дата-центре и доступен круглосуточно по интернету. У него есть:
- **IP-адрес** (например, `123.45.67.89`) — как адрес дома
- **Операционная система** (в нашем случае — Ubuntu 22.04)
- **Логин и пароль** — чтобы вы могли к нему подключаться

Вы платите за аренду от 200 до 500 рублей в месяц (в зависимости от провайдера и конфигурации).

**Известные провайдеры VPS:**
| Провайдер | Цена (примерно) | Оплата из РФ |
|---|---|---|
| [Aeza](https://aeza.net) | от 3 €/мес | ✅ да |
| [PQ Hosting](https://pq.hosting) | от 2 €/мес | ✅ да |
| [VDSina](https://vdsina.com) | от 250 ₽/мес | ✅ да |
| [Timeweb Cloud](https://timeweb.cloud) | от 200 ₽/мес | ✅ да |
| [Hetzner](https://hetzner.com) | от 4 €/мес | ⚠️ нужна зарубежная карта |
| [DigitalOcean](https://digitalocean.com) | от 6 $/мес | ⚠️ нужна зарубежная карта |

> **Совет для новичка:** если вы в России — выбирайте российского провайдера для **первого** сервера (SERVER-A) и зарубежного для **второго** (SERVER-B). Российский будет «входной точкой» — до него ТСПУ не блокирует трафик, потому что он внутри страны. Зарубежный — «выходной», через него трафик пойдёт в открытый интернет.

---

## 🌍 Почему нужны **два** сервера (каскад)

Можно поставить VPN и на одном сервере. Но тогда:
- Если сервер в Германии — ТСПУ со временем может увидеть, что вы подключаетесь к зарубежному IP, и **блокирует** соединение.
- Если сервер в России — он выходит в интернет через российские сети, где те же блокировки.

**Каскад** решает эту проблему: у вас **два** сервера, соединённых между собой шифрованным туннелем.


# Каскадный VPN на AmneziaWG 3.1
## 📱 Какое приложение использовать на клиенте

Этот проект разворачивает серверную часть на протоколе **AmneziaWG** — это форк WireGuard с обфускацией, устойчивой к DPI и ТСПУ. Для подключения к развёрнутому VPN клиенту понадобится **одно из двух приложений**:

| Приложение | Платформы | Ссылка |
|---|---|---|
| **AmneziaVPN** *(рекомендуется)* | Windows, macOS, Linux, Android, iOS | https://amnezia.org/ru/downloads https://storage.googleapis.com/amnezia/amnezia.org |
| **AmneziaWG** | Windows, macOS, Linux, Android, iOS | https://github.com/amnezia-vpn/amneziawg-go/releases  https://storage.googleapis.com/amnezia/amnezia.org|

**Что выбрать:**

- **AmneziaVPN** — универсальное приложение, умеет работать со всеми протоколами (AmneziaWG, OpenVPN, WireGuard, XRay). Поддерживает импорт `vpn://` ссылок и QR-кодов. **Рекомендуется для большинства пользователей.**
- **AmneziaWG** — более лёгкое приложение только под протокол AmneziaWG. Меньше потребляет ресурсов. Подходит для слабых устройств или если нужен только этот протокол.

**Как передать конфиг клиенту:** после создания клиента скрипт или веб-панель выдадут вам **одно из трёх**:

1. `.conf` файл — универсальный, подходит для обоих приложений и любого WireGuard-совместимого клиента.
2. `vpn://` ссылка — только для AmneziaVPN. Импортируется через **➕ → Импорт из буфера обмена**.
3. QR-код — удобно для мобильных: отсканировать в приложении через **➕ → Сканировать QR-код**.

> **Важно:** клиент должен использовать **тот же протокол** (AmneziaWG), под который настроен сервер. Если в конфиге указан `AmneziaWG 3.1`, а клиент попытается подключиться по обычному WireGuard — соединения не будет.

---

Готовое решение для развёртывания **каскадного VPN** (двойной туннель) между двумя серверами VPS с обфускацией протокола AmneziaWG 3.1.

Клиент подключается к **первому серверу** (обычно в стране с блокировками), трафик идёт через **второй сервер** (в свободной стране) и только потом в открытый интернет. Оба плеча туннеля защищены шифрованием и обфускацией, устойчивой к DPI/ТСПУ.

```
┌────────┐      ┌─────────────────┐      ┌──────────────────┐      ┌──────────┐
│ Client │ ───► │  SERVER-A       │ ───► │  SERVER-B        │ ───► │ Internet │
│ (Amnz) │      │  (страна X)     │      │  (страна Y)      │      │          │
└────────┘ UDP  │  awg1 → awg0    │ UDP  │  awg0 → NAT      │      └──────────┘
          :585  └─────────────────┘ :8443└──────────────────┘
```

## Что даёт

- ✅ Обход блокировок и DPI
- ✅ Протокол AmneziaWG 3.1 (расширение WireGuard с обфускацией)
- ✅ Двойной каскад — трафик выходит из другой страны
- ✅ Уникальные параметры обфускации для каждого клиента
- ✅ Готовые скрипты управления клиентами (добавить/удалить/список)
- ✅ Веб-панель с авторизацией (опционально)
- ✅ Генерация `vpn://` ссылок и QR-кодов для AmneziaVPN

## Требования

**Два VPS-сервера** с Ubuntu 22.04 / Debian 12 или новее:

| Параметр | Минимум | Рекомендуется |
|---|---|---|
| ОС | Ubuntu 22.04 | Ubuntu 22.04 / 24.04 |
| RAM | 512 МБ | 1 ГБ |
| vCPU | 1 | 2 |
| Диск | 5 ГБ | 10 ГБ |
| Root-доступ | да | да |

Серверы должны **пинговать друг друга** напрямую (открытые порты UDP, без NAT-проблем).

## Архитектура

- **SERVER-A** — «входная точка». Слушает клиентов на порту **585/udp**.
- **SERVER-B** — «выходная точка». Слушает туннель от A на порту **8443/udp**.
- Между ними — **шифрованный туннель awg0** (внутренние IP `10.10.10.1` и `10.10.10.2`).
- Клиенты получают внутренние IP из подсети **10.8.0.0/24**.
- Клиентский трафик маршрутизируется: `клиент → SERVER-A → awg0 → SERVER-B → интернет`.

## Быстрый старт

### Вариант 1. Автоматический (рекомендуется)

```bash
# На SERVER-A (входной)
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/davideprikyan/amneziawg-cascade/main/deploy.sh -o /tmp/deploy.sh >/dev/null; cat /tmp/deploy.sh)"
```

Скрипт спросит:
- IP обоих серверов
- SSH-пароль от SERVER-B
- Имя сервера для отображения в AmneziaVPN

И развернёт всё автоматически.

### Вариант 2. Ручной

Смотрите разделы ниже.

---

## 📋 Пошаговая инструкция (ручной вариант)

### Шаг 1. Подготовка обоих серверов

Выполняется **на каждом** сервере (A и B).

```bash
# 1.1. Отключить автообновления (чтобы не мешали apt)
sudo systemctl stop unattended-upgrades apt-daily.timer apt-daily-upgrade.timer 2>/dev/null
sudo systemctl disable unattended-upgrades apt-daily.timer apt-daily-upgrade.timer 2>/dev/null

# 1.2. Установить зависимости
sudo apt update
sudo apt install -y software-properties-common python3-launchpadlib gnupg2 \
    linux-headers-$(uname -r) build-essential dkms iptables qrencode

# 1.3. Добавить PPA и установить AmneziaWG
sudo add-apt-repository -y ppa:amnezia/ppa
sudo apt update
sudo apt install -y amneziawg

# 1.4. Проверить установку
awg --version      # Должно быть "amneziawg-tools v3.1.xxxxxx"
sudo modprobe amneziawg
lsmod | grep amneziawg
echo "amneziawg" | sudo tee /etc/modules-load.d/amneziawg.conf

# 1.5. Включить IP-форвардинг
echo "net.ipv4.ip_forward=1" | sudo tee /etc/sysctl.d/99-amneziawg.conf
sudo sysctl --system
```

**Ожидаемо:** `awg --version` показывает `v3.1.*`.

---

### Шаг 2. Генерация уникальных параметров обфускации

> **Важно:** эти значения **должны быть уникальными** для каждой инсталляции. Не используйте значения из примеров.

Сохраните эту команду — она сгенерирует все нужные параметры разом.

**На любом из серверов:**

```bash
cat > /tmp/gen-params.sh <<'EOF'
#!/bin/bash
echo "# === Сгенерированные параметры обфускации ==="
echo "# Сохраните эти значения — они пойдут в конфиги!"
echo ""
echo "HeaderProtectionKey = $(awg genkey)"
echo ""
echo "# Junk packets (c — count, min/max — размер)"
echo "Jc = $((RANDOM % 6 + 3))"
JMIN=$((RANDOM % 40 + 10))
JMAX=$((JMIN + RANDOM % 40 + 20))
echo "Jmin = $JMIN"
echo "Jmax = $JMAX"
echo ""
echo "# S1-S4 (все >= 12, S1+56 != S2)"
while :; do
    S1=$((RANDOM % 50 + 12))
    S2=$((RANDOM % 50 + 12))
    [[ $((S1 + 56)) -ne $S2 ]] && break
done
echo "S1 = $S1"
echo "S2 = $S2"
echo "S3 = $((RANDOM % 50 + 12))"
echo "S4 = $((RANDOM % 50 + 12))"
echo ""
echo "# H1-H4 (уникальные диапазоны, каждый >= 10_000_000)"
python3 - <<'PY'
import random
ranges = []
while len(ranges) < 4:
    start = random.randint(5, 2_000_000_000)
    end = start + random.randint(10_000_000, 100_000_000)
    if end > 2_147_483_647: continue
    if all(end < r[0] or start > r[1] for r in ranges):
        ranges.append((start, end))
for i, (s, e) in enumerate(ranges, 1):
    print(f"H{i} = {s}-{e}")
PY
echo ""
echo "# Прочие параметры"
echo "ContentPaddingAddition = 16"
echo "RekeyAfterTime = 100-120"
echo "RekeyTimeout = 3-7"
echo "RejectAfterTime = 150-180"
echo "KeepaliveTimeout = 5-15"
echo "MaxHandshakeAttempts = 15-20"
echo "RandomTrailers = off"      # для межсерверного awg0
echo "DisableCookies = on"
EOF

bash /tmp/gen-params.sh | tee /tmp/params-awg0.txt
```

**Сохраните вывод.** Эти значения пойдут в конфиги `awg0.conf` на обоих серверах.

Отдельно сгенерируйте параметры для **клиентского интерфейса** (`awg1`). Они должны **отличаться** от параметров `awg0` — это повышает незаметность.

```bash
# Меняем RandomTrailers на on — для клиентов требуется
sed 's/RandomTrailers = off/RandomTrailers = on/' /tmp/params-awg0.txt > /tmp/params-awg1.txt
# Генерируем новые значения (повторный запуск скрипта)
bash /tmp/gen-params.sh | sed 's/RandomTrailers = off/RandomTrailers = on/' > /tmp/params-awg1.txt
cat /tmp/params-awg1.txt
```

---

### Шаг 3. Настройка межсерверного туннеля (awg0)

#### 3.1. Генерация ключей (на каждом сервере)

**На SERVER-A:**

```bash
sudo mkdir -p /etc/amnezia/amneziawg
cd /etc/amnezia/amneziawg
sudo sh -c 'umask 077; awg genkey > private.key'
sudo sh -c 'awg pubkey < private.key > public.key'
sudo chmod 600 private.key
sudo chmod 644 public.key
echo "SERVER-A public key: $(sudo cat public.key)"
```

**На SERVER-B:**

```bash
sudo mkdir -p /etc/amnezia/amneziawg
cd /etc/amnezia/amneziawg
sudo sh -c 'umask 077; awg genkey > private.key'
sudo sh -c 'awg pubkey < private.key > public.key'
sudo chmod 600 private.key
sudo chmod 644 public.key
echo "SERVER-B public key: $(sudo cat public.key)"
```

**Сохраните оба публичных ключа** — они понадобятся друг другу.

#### 3.2. Конфиг `awg0.conf` на SERVER-B (выходной)

Создайте файл:

```bash
sudo nano /etc/amnezia/amneziawg/awg0.conf
```

Вставьте (заменив `<...>` на свои значения):

```ini
[Interface]
PrivateKey = <SERVER_B_PRIVATE_KEY>
Address = 10.10.10.1/32
ListenPort = 8443
MTU = 1180
Jc = <Jc>
Jmin = <Jmin>
Jmax = <Jmax>
S1 = <S1>
S2 = <S2>
S3 = <S3>
S4 = <S4>
H1 = <H1>
H2 = <H2>
H3 = <H3>
H4 = <H4>
HeaderProtectionKey = <HeaderProtectionKey>
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
PostUp = iptables -t nat -A POSTROUTING -o <ВНЕШНИЙ_ИНТЕРФЕЙС> -j MASQUERADE
PostUp = while iptables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1000 2>/dev/null; do :; done
PostUp = iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1000
PostDown = iptables -D FORWARD -i awg0 -j ACCEPT
PostDown = iptables -D FORWARD -o awg0 -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -o <ВНЕШНИЙ_ИНТЕРФЕЙС> -j MASQUERADE
PostDown = iptables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1000 2>/dev/null || true

[Peer]
PublicKey = <SERVER_A_PUBLIC_KEY>
AllowedIPs = 10.10.10.2/32
```

**Как узнать имя внешнего интерфейса:**

```bash
ip route get 8.8.8.8 | awk '{print $5}' | head -1
```

Обычно это `eth0`, `ens3`, `enp1s0` или `ens18`.

Права:

```bash
sudo chmod 600 /etc/amnezia/amneziawg/awg0.conf
```

#### 3.3. Конфиг `awg0.conf` на SERVER-A (входной)

```bash
sudo nano /etc/amnezia/amneziawg/awg0.conf
```

Вставьте:

```ini
[Interface]
PrivateKey = <SERVER_A_PRIVATE_KEY>
Address = 10.10.10.2/32
ListenPort = 8443
MTU = 1180
Table = off
Jc = <Jc>
Jmin = <Jmin>
Jmax = <Jmax>
S1 = <S1>
S2 = <S2>
S3 = <S3>
S4 = <S4>
H1 = <H1>
H2 = <H2>
H3 = <H3>
H4 = <H4>
HeaderProtectionKey = <HeaderProtectionKey>
ContentPaddingAddition = 16
RekeyAfterTime = 100-120
RekeyTimeout = 3-7
RejectAfterTime = 150-180
KeepaliveTimeout = 5-15
MaxHandshakeAttempts = 15-20
RandomTrailers = off
DisableCookies = on
PostUp = ip route add 10.10.10.1/32 dev awg0
PostUp = ip route add default dev awg0 table 100
PostUp = ip rule add from 10.8.0.0/24 lookup 100
PostUp = iptables -A FORWARD -i awg1 -o awg0 -j ACCEPT
PostUp = iptables -A FORWARD -i awg0 -o awg1 -j ACCEPT
PostUp = iptables -t nat -A POSTROUTING -o awg0 -j MASQUERADE
PostUp = while iptables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1000 2>/dev/null; do :; done
PostUp = iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1000
PostDown = ip rule del from 10.8.0.0/24 lookup 100
PostDown = ip route del default dev awg0 table 100
PostDown = ip route del 10.10.10.1/32 dev awg0
PostDown = iptables -D FORWARD -i awg1 -o awg0 -j ACCEPT
PostDown = iptables -D FORWARD -i awg0 -o awg1 -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -o awg0 -j MASQUERADE
PostDown = iptables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1000 2>/dev/null || true

[Peer]
PublicKey = <SERVER_B_PUBLIC_KEY>
Endpoint = <SERVER_B_IP>:8443
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
```

Права:

```bash
sudo chmod 600 /etc/amnezia/amneziawg/awg0.conf
```

#### 3.4. Запуск и проверка

**На SERVER-B:**

```bash
sudo systemctl enable awg-quick@awg0
sudo systemctl restart awg-quick@awg0
sudo systemctl status awg-quick@awg0 --no-pager | head -10
```

**На SERVER-A:**

```bash
sudo systemctl enable awg-quick@awg0
sudo systemctl restart awg-quick@awg0
sudo systemctl status awg-quick@awg0 --no-pager | head -10
sleep 3
ping -c 3 10.10.10.1
```

**Ожидаемо:** обе службы `active (exited)`, ping идёт, `awg show awg0` показывает `latest handshake`.

---

### Шаг 4. Клиентский интерфейс awg1 (только на SERVER-A)

#### 4.1. Генерация ключей awg1

```bash
cd /etc/amnezia/amneziawg
sudo sh -c 'umask 077; awg genkey > awg1_private.key'
sudo sh -c 'awg pubkey < awg1_private.key > awg1_public.key'
sudo chmod 600 awg1_private.key
sudo chmod 644 awg1_public.key
echo "awg1 public key: $(sudo cat awg1_public.key)"
```

#### 4.2. Конфиг `awg1.conf`

```bash
sudo nano /etc/amnezia/amneziawg/awg1.conf
```

Вставьте (значения — из `/tmp/params-awg1.txt`):

```ini
[Interface]
PrivateKey = <AWG1_PRIVATE_KEY>
Address = 10.8.0.1/24
ListenPort = 585
MTU = 1100
Jc = <Jc>
Jmin = <Jmin>
Jmax = <Jmax>
S1 = <S1>
S2 = <S2>
S3 = <S3>
S4 = <S4>
H1 = <H1>
H2 = <H2>
H3 = <H3>
H4 = <H4>
HeaderProtectionKey = <HeaderProtectionKey>
ContentPaddingAddition = 16
RekeyAfterTime = 100-120
RekeyTimeout = 3-7
RejectAfterTime = 150-180
KeepaliveTimeout = 5-15
MaxHandshakeAttempts = 15-20
RandomTrailers = on
DisableCookies = on
```

Права и запуск:

```bash
sudo chmod 600 /etc/amnezia/amneziawg/awg1.conf
sudo systemctl enable awg-quick@awg1
sudo systemctl restart awg-quick@awg1
sudo systemctl status awg-quick@awg1 --no-pager | head -10
```

**Проверка маршрутизации:**

```bash
ip rule show     # должно быть: from 10.8.0.0/24 lookup 100
ip route show table 100    # должно быть: default dev awg0
```

---

### Шаг 5. Первый клиент

#### 5.1. Генерация ключей клиента

```bash
cd /etc/amnezia/amneziawg
sudo sh -c 'umask 077; awg genkey > client1_private.key'
sudo sh -c 'awg pubkey < client1_private.key > client1_public.key'
echo "Client private: $(sudo cat client1_private.key)"
echo "Client public: $(sudo cat client1_public.key)"
```

#### 5.2. Добавить клиента в awg1.conf

```bash
sudo nano /etc/amnezia/amneziawg/awg1.conf
```

В конец файла добавьте:

```ini
[Peer]
# Client: admin
PublicKey = <CLIENT1_PUBLIC_KEY>
AllowedIPs = 10.8.0.2/32
```

Перезапуск:

```bash
sudo systemctl restart awg-quick@awg1
sudo awg show awg1
```

#### 5.3. Клиентский `.conf` файл

Создайте файл `client1.conf` на своём ПК:

```ini
[Interface]
PrivateKey = <CLIENT1_PRIVATE_KEY>
Address = 10.8.0.2/32
MTU = 1280
DNS = 1.1.1.1, 1.0.0.1
Jc = <Jc>
Jmin = <Jmin>
Jmax = <Jmax>
S1 = <S1>
S2 = <S2>
S3 = <S3>
S4 = <S4>
H1 = <H1>
H2 = <H2>
H3 = <H3>
H4 = <H4>
HeaderProtectionKey = <HeaderProtectionKey>
ContentPaddingAddition = 16
RekeyAfterTime = 100-120
RekeyTimeout = 3-7
RejectAfterTime = 150-180
KeepaliveTimeout = 5-15
MaxHandshakeAttempts = 15-20
RandomTrailers = on
DisableCookies = on
I1 = <r 2><b 0x8580000100010000000006676f6f676c6503636f6d0000010001c00c000100010000105a0004xxxxxxxx>

[Peer]
PublicKey = <AWG1_PUBLIC_KEY>
Endpoint = <SERVER_A_IP>:585
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
```

Значение `I1` — **уникальное для каждого клиента**. Генерируется командой (см. раздел ниже).

---

## 🛠️ Генерация I1 для клиента

`I1` — это «маскировочный» пакет, выглядит для DPI как DNS-ответ. Для каждого клиента должен быть **свой**.

```bash
python3 - <<'PYEOF'
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
print(f"I1 = <r 2><b 0x{h}>")
PYEOF
```

---

## 📦 Управление клиентами (скрипты)

Репозиторий содержит 3 скрипта в папке `scripts/`:

### `amnezia-add-client.sh` — добавить клиента

```bash
sudo ./scripts/amnezia-add-client.sh <имя>
```

Что делает:
- Находит свободный IP в `10.8.0.0/24`
- Генерирует пару ключей + уникальный `I1`
- Создаёт `/root/clients/<имя>.conf`
- Добавляет `[Peer]` в `awg1.conf`
- Перезапускает `awg-quick@awg1`
- Опционально: конвертирует в `vpn://` ссылку для AmneziaVPN
- Показывает QR-код

### `amnezia-remove-client.sh` — удалить клиента

```bash
sudo ./scripts/amnezia-remove-client.sh <имя>
```

Или без аргумента — откроется меню со списком клиентов.

### `amnezia-list-clients.sh` — список клиентов

```bash
sudo ./scripts/amnezia-list-clients.sh
```

Показывает таблицу: имя, IP, статус (ONLINE/OFFLINE/NEVER), RX/TX, время последнего handshake.

**Как читать статистику:**
- **RX (от клиента)** — клиент отправил через VPN (его upload)
- **TX (клиенту)** — сервер отправил клиенту (его download)
- **ONLINE** — handshake < 3 минут назад
- **OFFLINE** — handshake старше 3 минут
- **NEVER** — клиент ни разу не подключался

---

## 🌐 Веб-панель (опционально)

Развёртывание панели на SERVER-A:

```bash
# Установить зависимости Python
sudo apt install -y python3-venv
sudo python3 -m venv /opt/amnezia-panel-venv
sudo /opt/amnezia-panel-venv/bin/pip install fastapi 'uvicorn[standard]' python-multipart qrcode

# Создать файл конфигурации с логином/паролем
sudo tee /etc/amnezia-panel.conf > /dev/null <<EOF
username=admin
password=$(openssl rand -base64 24)
EOF
sudo chmod 600 /etc/amnezia-panel.conf

# Показать сгенерированный пароль
sudo cat /etc/amnezia-panel.conf

# Скопировать amnezia-panel.py и service из репозитория
sudo cp web-panel/amnezia-panel.py /opt/
sudo cp web-panel/amnezia-panel.service /etc/systemd/system/

# ВНИМАНИЕ: в amnezia-panel.py замените SERVER_LABEL на своё имя

sudo systemctl daemon-reload
sudo systemctl enable --now amnezia-panel
```

Панель будет доступна по адресу `http://<SERVER_A_IP>:8000`.

> **Безопасность:** перед публичным использованием настройте HTTPS (см. `web-panel/README.md`).

**Возможности панели:**
- 🔐 Basic Auth логин/пароль
- 👥 Список клиентов с трафиком и статусами (обновление каждые 5 сек)
- ➕ Создание / 🗑️ удаление клиентов
- 📥 Скачивание `.conf` и `vpn://`
- 🔲 QR-код `vpn://` прямо в браузере
- 🔄 Перезапуск туннелей одной кнопкой
- 🌙 Тёмная тема
- 📊 Суммарный RX/TX в шапке

---

## 🐛 Диагностика

### Туннель awg0 не поднимается

```bash
sudo journalctl -xeu awg-quick@awg0 -n 50 --no-pager
sudo awg show awg0
```

Частые причины:
- Не совпадают `HeaderProtectionKey`, `S1-S4`, `H1-H4` на двух серверах
- Не совпадают `RandomTrailers` (должны быть одинаковыми в одном туннеле)
- Порт 8443/udp заблокирован у провайдера
- Неправильно указан `Endpoint` (IP или порт)

### Клиент подключается, но нет интернета

Проверьте на SERVER-A:

```bash
ip rule show               # должно быть from 10.8.0.0/24 lookup 100
ip route show table 100    # должно быть default dev awg0
```

На SERVER-B:

```bash
sudo iptables -t nat -L POSTROUTING -n -v    # MASQUERADE должен быть
```

### Медленная скорость / upload 0

Проблема с MTU/MSS. Решение:
- На обоих серверах `MTU = 1180` (для awg0) и `MTU = 1100` (для awg1)
- MSS-клэмп `--set-mss 1000` на обоих серверах (правила в PostUp/PostDown)
- Клиентский `MTU = 1280`
- Убедитесь, что `RandomTrailers` на клиенте и `awg1` **совпадают**

Если потери при больших пакетах — уменьшите MTU/MSS ещё на 40-80 байт.

### Панель не запускается

```bash
sudo journalctl -u amnezia-panel -n 30 --no-pager
```

Проверьте кодировку файла:

```bash
file -i /opt/amnezia-panel.py   # должно быть charset=utf-8
```

> **Важно:** файлы для Python (`.py`) сохраняйте **только в UTF-8**. Если редактируете через Windows-редакторы (MobaXterm, Notepad), выберите UTF-8 в настройках кодировки.

---

## ❓ FAQ

**Можно ли использовать один сервер?**  
Да, но тогда нет смысла в каскаде — используйте чистый AmneziaWG. Каскад нужен для «разрыва» между странами.

**Можно ли добавить третий сервер?**  
Технически да — добавьте `awg2` на SERVER-B и свяжите его с сервером C по аналогии с awg0. Понадобится обновить маршрутизацию.

**Сколько клиентов можно создать?**  
До 253 (подсеть `/24`), но реально зависит от нагрузки на сервер. Каждый активный клиент потребляет CPU на шифрование.

**Что если IP сервера статический, а клиента — динамический?**  
Ничего страшного — `PersistentKeepalive = 25` поддерживает соединение через NAT.

**Как забэкапить всё?**  
Конфиги лежат в `/etc/amnezia/amneziawg/`. Клиенты — в `/root/clients/`. Достаточно скопировать эти две папки.

---

## 🔧 Troubleshooting (для опытных)

Здесь собраны реальные проблемы, с которыми сталкивались при развёртывании, и способы их решения.

---

### 1. Туннель не устанавливает handshake, но публичный ping идёт

**Симптомы:**
- `ping <PUBLIC_IP_B>` с SERVER-A — OK, 49 мс.
- `ping 10.10.10.1` — 100% потерь.
- `sudo awg show awg0` на обоих показывает `transfer: 0 B received, N MiB sent` (или наоборот).
- `latest handshake` отсутствует.

**Причина:** три варианта по вероятности.

**1.1. Разошлись параметры обфускации**

Сверьте на **обоих** серверах:

```bash
sudo grep -E "^(HeaderProtectionKey|S1|S2|S3|S4|H1|H2|H3|H4|RandomTrailers|DisableCookies)" /etc/amnezia/amneziawg/awg0.conf | sort

--- 
## 📜 Лицензия

MIT. Используйте свободно.

## 🤝 Вклад

PR и issues приветствуются. Особенно интересны:
- Автоматизация деплоя (Ansible, Terraform)
- Мониторинг (Prometheus exporter, Grafana dashboard)
- Telegram-бот для уведомлений
- Поддержка Alpine Linux

## 🙏 Благодарности

- [Amnezia VPN](https://amnezia.org) — за протокол и инструменты
- [WireGuard](https://www.wireguard.com) — за основу
- Сообществу за идеи и отчёты об ошибках
