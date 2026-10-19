#!/usr/bin/env python3
import os
import re
import sqlite3
import subprocess
import time
import sys

# ===== Проверка состояния =====
STATE_FILE = "/var/lib/amnezia-panel/traffic_enabled"
os.makedirs("/var/lib/amnezia-panel", exist_ok=True)
if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE) as _f:
            if _f.read().strip() == "0":
                sys.exit(0)  # сбор отключён
    except Exception:
        pass

DB_DIR = "/var/lib/amnezia-panel"
DB = f"{DB_DIR}/traffic.db"
CONF = "/etc/amnezia/amneziawg/awg1.conf"
RETAIN_DAYS = 90

os.makedirs(DB_DIR, exist_ok=True)

conn = sqlite3.connect(DB)
conn.execute("""
CREATE TABLE IF NOT EXISTS traffic (
    ts INTEGER NOT NULL,
    pubkey TEXT NOT NULL,
    name TEXT,
    rx_total INTEGER NOT NULL,
    tx_total INTEGER NOT NULL,
    PRIMARY KEY (ts, pubkey)
);
""")
conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_name ON traffic(name, ts);")
conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_ts ON traffic(ts);")

# Парсим awg1.conf, чтобы получить карту pubkey -> имя клиента
names = {}
if os.path.exists(CONF):
    with open(CONF, encoding="utf-8") as f:
        content = f.read()
    for block in re.split(r"\n(?=\[Peer\])", content):
        if "[Peer]" not in block:
            continue
        nm = re.search(r"#\s*Client:\s*(\S+)", block)
        pk = re.search(r"PublicKey\s*=\s*(\S+)", block)
        if nm and pk:
            names[pk.group(1)] = nm.group(1)

def parse_bytes(s):
    """Парсит строку вида '55.88 MiB' или '0 B' в целые байты."""
    if not s:
        return 0
    m = re.match(r"^\s*([\d.]+)\s*([KMG]?i?B|B)\s*$", str(s), re.I)
    if not m:
        return 0
    v = float(m.group(1))
    u = m.group(2).upper().replace("I", "")
    return int(v * {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}.get(u, 1))

# Используем awg show awg1 (человеческий формат) — как в самой панели
try:
    r = subprocess.run(["awg", "show", "awg1"], capture_output=True,
                       text=True, timeout=15, check=True)
except Exception as e:
    print(f"ERROR: failed to run awg show: {e}")
    sys.exit(1)

stats = {}
cur = None
for line in r.stdout.splitlines():
    s = line.strip()
    m = re.match(r"^peer:\s*(\S+)", s)
    if m:
        cur = m.group(1)
        stats[cur] = {"rx": 0, "tx": 0}
        continue
    if not cur:
        continue
    m = re.match(r"^transfer:\s*(.+?)\s+received,\s*(.+?)\s+sent", s)
    if m:
        stats[cur]["rx"] = parse_bytes(m.group(1))
        stats[cur]["tx"] = parse_bytes(m.group(2))

ts = int(time.time())
for pub, s in stats.items():
    name = names.get(pub, "unknown")
    conn.execute(
        "INSERT OR REPLACE INTO traffic(ts,pubkey,name,rx_total,tx_total) VALUES(?,?,?,?,?)",
        (ts, pub, name, s["rx"], s["tx"])
    )

# Очистка старых данных (раз в сутки)
if ts % 86400 < 300:
    conn.execute("DELETE FROM traffic WHERE ts < ?", (ts - RETAIN_DAYS * 86400,))

conn.commit()
conn.close()