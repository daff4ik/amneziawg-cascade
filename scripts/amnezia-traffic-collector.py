#!/usr/bin/env python3
import os
import re
import sqlite3
import subprocess
import time
import sys

DB_DIR = "/var/lib/amnezia-panel"
DB = f"{DB_DIR}/traffic.db"
CONF = "/etc/amnezia/amneziawg/awg1.conf"

os.makedirs(DB_DIR, exist_ok=True)

conn = sqlite3.connect(DB)
# Новая таблица для накопительной статистики
conn.execute("""
CREATE TABLE IF NOT EXISTS client_stats (
    pubkey TEXT PRIMARY KEY,
    name TEXT,
    total_rx INTEGER DEFAULT 0,
    total_tx INTEGER DEFAULT 0,
    last_rx INTEGER DEFAULT 0,
    last_tx INTEGER DEFAULT 0,
    last_handshake INTEGER DEFAULT 0
);
""")

# Получаем имена клиентов из конфига
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

# Используем dump для получения точных цифр (байты) и unix timestamp handshake
try:
    out = subprocess.check_output(["awg", "show", "awg1", "dump"], text=True, timeout=10)
except Exception as e:
    print(f"ERROR: failed to run awg show dump: {e}")
    sys.exit(1)

cur = conn.cursor()
now_ts = int(time.time())

# Первая строка (интерфейс) пропускается
for line in out.strip().split("\n")[1:]:
    parts = line.split("\t")
    if len(parts) < 8:
        continue
    
    pub = parts[0]
    handshake_ts = int(parts[4]) # Unix timestamp или 0
    rx_bytes = int(parts[5])
    tx_bytes = int(parts[6])
    name = names.get(pub, "unknown")

    cur.execute("SELECT total_rx, total_tx, last_rx, last_tx, last_handshake FROM client_stats WHERE pubkey=?", (pub,))
    row = cur.fetchone()

    if row:
        total_rx, total_tx, last_rx, last_tx, old_handshake = row
        
        # Вычисляем дельту (если awg перезапускался, счетчики могли сброситься)
        d_rx = rx_bytes - last_rx if rx_bytes >= last_rx else rx_bytes
        d_tx = tx_bytes - last_tx if tx_bytes >= last_tx else tx_bytes
        
        total_rx += d_rx
        total_tx += d_tx
        
        # Сохраняем самый свежий handshake
        new_handshake = max(old_handshake, handshake_ts) if handshake_ts > 0 else old_handshake

        cur.execute("""
            UPDATE client_stats 
            SET name=?, total_rx=?, total_tx=?, last_rx=?, last_tx=?, last_handshake=?
            WHERE pubkey=?
        """, (name, total_rx, total_tx, rx_bytes, tx_bytes, new_handshake, pub))
    else:
        # Первое появление клиента
        cur.execute("""
            INSERT INTO client_stats (pubkey, name, total_rx, total_tx, last_rx, last_tx, last_handshake)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (pub, name, rx_bytes, tx_bytes, rx_bytes, tx_bytes, handshake_ts))

# Очищаем БД от тех, кто удален из awg1.conf (чтобы не копился мусор)
if names:
    placeholders = ','.join('?' * len(names))
    cur.execute(f"DELETE FROM client_stats WHERE pubkey NOT IN ({placeholders})", tuple(names.keys()))

conn.commit()
conn.close()