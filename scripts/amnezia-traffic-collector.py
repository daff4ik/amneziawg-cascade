#!/usr/bin/env python3
import os
import re
import sqlite3
import subprocess
import time

# ===== Проверка состояния =====
STATE_FILE = "/var/lib/amnezia-panel/traffic_enabled"
os.makedirs("/var/lib/amnezia-panel", exist_ok=True)
if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE) as _f:
            if _f.read().strip() == "0":
                exit(0)  # сбор отключён
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

# Получаем текущий трафик через awg
try:
    out = subprocess.check_output(["awg", "show", "awg1", "dump"], text=True)
except Exception:
    exit(1)

ts = int(time.time())
for line in out.strip().split("\n")[1:]:
    parts = line.split("\t")
    if len(parts) < 8:
        continue
    pub = parts[0]
    rx = parts[5]
    tx = parts[6]
    name = names.get(pub, "unknown")
    conn.execute(
        "INSERT OR REPLACE INTO traffic(ts,pubkey,name,rx_total,tx_total) VALUES(?,?,?,?,?)",
        (ts, pub, name, int(rx), int(tx))
    )

# Очистка старых данных (раз в сутки)
if ts % 86400 < 300:
    conn.execute("DELETE FROM traffic WHERE ts < ?", (ts - RETAIN_DAYS * 86400,))

conn.commit()
conn.close()