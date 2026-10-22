#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AmneziaWG Web Panel — с управлением и переключением SERVER B."""
import os
import re
import io
import hmac
import hashlib
import secrets
import sqlite3
import subprocess
import time
import urllib.request
import json
import datetime
import shutil
from fastapi import FastAPI, HTTPException, Depends, Form, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response, JSONResponse
from starlette.concurrency import run_in_threadpool
import uvicorn

CONFIG_FILE   = "/etc/amnezia-panel.conf"
AWG_IFACE     = "awg1"
AWG_CONF      = f"/etc/amnezia/amneziawg/{AWG_IFACE}.conf"
AWG0_CONF     = "/etc/amnezia/amneziawg/awg0.conf"
AWG0_PUBKEY   = "/etc/amnezia/amneziawg/public.key"
CLIENTS_DIR   = "/root/clients"
ADD_SCRIPT    = "/usr/local/bin/amnezia-add-client.sh"
REMOVE_SCRIPT = "/usr/local/bin/amnezia-remove-client.sh"
NOTIFY_SCRIPT = "/usr/local/bin/amnezia-notify.sh"
TRAFFIC_DB    = "/var/lib/amnezia-panel/traffic.db"
SERVERS_FILE  = "/var/lib/amnezia-panel/servers.json"
SERVER_LABEL  = "__SERVER_LABEL__"
COMMIT_STATE_FILE = "/var/lib/amnezia-panel/installed_commit"
REPO_API_URL      = "https://api.github.com/repos/daff4ik/amneziawg-cascade/commits"

SESSION_COOKIE = "amnezia_session"
SESSION_DAYS   = 7

# ================ Auth ================
def load_credentials():
    if not os.path.exists(CONFIG_FILE):
        raise RuntimeError("no config")
    cfg = {}
    with open(CONFIG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg.get("username", "admin"), cfg.get("password", "admin")

def make_token(p: str) -> str:
    return hmac.new(p.encode(), b"amnezia-panel-session", hashlib.sha256).hexdigest()

class RedirectToLogin(Exception):
    pass

def current_user(request: Request) -> str:
    try:
        u, p = load_credentials()
    except RuntimeError:
        raise HTTPException(500, "config error")
    t = request.cookies.get(SESSION_COOKIE, "")
    if not hmac.compare_digest(t, make_token(p)):
        if request.url.path.startswith("/api/"):
            raise HTTPException(401, "Unauthorized")
        raise RedirectToLogin()
    return u

# ================ Servers (JSON) ================
def load_servers():
    if not os.path.exists(SERVERS_FILE):
        return {"active_id": None, "servers": []}
    try:
        with open(SERVERS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"active_id": None, "servers": []}

def save_servers(data):
    os.makedirs(os.path.dirname(SERVERS_FILE), exist_ok=True)
    with open(SERVERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def ensure_current_server_in_list():
    data = load_servers()
    if data.get("servers"):
        return
    try:
        if not os.path.exists(AWG0_CONF):
            return
        with open(AWG0_CONF, encoding="utf-8") as f:
            conf = f.read()
        m = re.search(r"^Endpoint\s*=\s*([\d.]+):(\d+)", conf, re.MULTILINE)
        if m:
            ip = m.group(1)
            data["servers"].append({
                "id": 1, "ip": ip, "port": 22, "user": "root",
                "name": f"Current ({ip})"
            })
            data["active_id"] = 1
            save_servers(data)
    except Exception:
        pass

# ================ Update Check ================
def get_installed_commit():
    if os.path.exists(COMMIT_STATE_FILE):
        with open(COMMIT_STATE_FILE) as f:
            return f.read().strip()
    return ""

def check_github_updates():
    installed_sha = get_installed_commit()
    try:
        req = urllib.request.Request(REPO_API_URL + "?per_page=15", headers={'User-Agent': 'Amnezia-Panel'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            commits = json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

    new_commits = []
    for c in commits:
        sha = c["sha"]
        if sha == installed_sha:
            break
        msg = c["commit"]["message"]
        date = c["commit"]["author"]["date"]
        new_commits.append({"sha": sha, "message": msg, "date": date})

    return {
        "has_updates": len(new_commits) > 0,
        "commits": new_commits[:10],
        "latest_sha": commits[0]["sha"] if commits else ""
    }

# ================ SSH helpers ================
def ssh_exec(ip, port, user, password, command, timeout=60):
    cmd = ["sshpass", "-p", password, "ssh",
           "-o", "StrictHostKeyChecking=no",
           "-o", "UserKnownHostsFile=/dev/null",
           "-o", "ConnectTimeout=15",
           "-p", str(port), f"{user}@{ip}", command]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout, r.stderr
    except Exception as e:
        return False, "", str(e)

def ssh_upload(ip, port, user, password, local_path, remote_path, timeout=120):
    cmd = ["sshpass", "-p", password, "scp",
           "-o", "StrictHostKeyChecking=no",
           "-o", "UserKnownHostsFile=/dev/null",
           "-P", str(port), local_path, f"{user}@{ip}:{remote_path}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout, r.stderr
    except Exception as e:
        return False, "", str(e)

def install_awg_remote(ip, port, user, password):
    script = '''#!/bin/bash
export DEBIAN_FRONTEND=noninteractive
systemctl stop unattended-upgrades apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl disable unattended-upgrades apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
apt update -qq
apt install -y -qq software-properties-common python3-launchpadlib gnupg2 \\
    linux-headers-$(uname -r) build-essential dkms iptables qrencode
if ! command -v awg >/dev/null; then
    add-apt-repository -y ppa:amnezia/ppa
    apt update -qq
    apt install -y -qq amneziawg
fi
modprobe amneziawg || true
echo "amneziawg" > /etc/modules-load.d/amneziawg.conf
echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-amneziawg.conf
sysctl --system >/dev/null 2>&1 || true
echo INSTALLED_OK
'''
    lp = "/tmp/_install_awg.sh"
    with open(lp, "w") as f:
        f.write(script)
    try:
        ok, _, err = ssh_upload(ip, port, user, password, lp, "/tmp/_install_awg.sh")
        if not ok:
            return False, "", err
        ok, out, err = ssh_exec(ip, port, user, password, "bash /tmp/_install_awg.sh", timeout=300)
        ssh_exec(ip, port, user, password, "rm -f /tmp/_install_awg.sh", timeout=10)
        return ok, out, err
    finally:
        try: os.remove(lp)
        except: pass

def build_de_conf(ru_conf_path, de_privkey, ru_pubkey, de_iface):
    with open(ru_conf_path, encoding="utf-8") as f:
        ru_conf = f.read()
    def get(param):
        m = re.search(rf"^{param}\s*=\s*(.+)$", ru_conf, re.MULTILINE)
        return m.group(1).strip() if m else None
    param_keys = ["Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4",
                  "HeaderProtectionKey", "ContentPaddingAddition", "RekeyAfterTime",
                  "RekeyTimeout", "RejectAfterTime", "KeepaliveTimeout",
                  "MaxHandshakeAttempts", "RandomTrailers", "DisableCookies"]
    params = {}
    for p in param_keys:
        v = get(p)
        if v:
            params[p] = v
    params_str = "\n".join(f"{k} = {v}" for k, v in params.items())
    return f"""[Interface]
PrivateKey = {de_privkey}
Address = 10.10.10.1/32
ListenPort = 8443
MTU = 1180
{params_str}
PostUp = iptables -A FORWARD -i awg0 -j ACCEPT
PostUp = iptables -A FORWARD -o awg0 -j ACCEPT
PostUp = iptables -t nat -A POSTROUTING -o {de_iface} -j MASQUERADE
PostUp = while iptables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1000 2>/dev/null; do :; done
PostUp = iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1000
PostDown = iptables -D FORWARD -i awg0 -j ACCEPT
PostDown = iptables -D FORWARD -o awg0 -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -o {de_iface} -j MASQUERADE
PostDown = iptables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss 1000 2>/dev/null || true

[Peer]
PublicKey = {ru_pubkey}
AllowedIPs = 10.10.10.2/32
"""

def update_ru_peer(conf_text, de_pub, de_ip):
    lines = conf_text.split("\n")
    out = []
    in_peer = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[Peer]":
            in_peer = True
            out.append(line)
            continue
        if in_peer and stripped.startswith("["):
            in_peer = False
        if in_peer:
            if stripped.startswith("PublicKey"):
                out.append(f"PublicKey = {de_pub}")
                continue
            if stripped.startswith("Endpoint"):
                out.append(f"Endpoint = {de_ip}:8443")
                continue
        out.append(line)
    return "\n".join(out)

def do_switch_server(server, password):
    ip, port, user = server["ip"], server["port"], server["user"]
    name = server.get("name", ip)

    # 1. SSH check
    ok, out, err = ssh_exec(ip, port, user, password, "echo SSH_OK")
    if not ok or "SSH_OK" not in (out or ""):
        return False, f"SSH до {ip} не работает: {err or out}"

    # 2. Install AmneziaWG on new DE
    ok, out, err = install_awg_remote(ip, port, user, password)
    if not ok or "INSTALLED_OK" not in (out or ""):
        return False, f"Установка AmneziaWG не удалась: {(err or out)[-300:]}"

    # 3. Detect external iface
    ok, out, _ = ssh_exec(ip, port, user, password,
                          "ip route get 8.8.8.8 | awk '{print $5}' | head -1")
    de_iface = (out or "").strip().split("\n")[0]
    if not de_iface:
        return False, "Не удалось определить внешний интерфейс на новом DE"

    # 4. Generate new DE keypair
    r = subprocess.run(["awg", "genkey"], capture_output=True, text=True, check=True)
    de_priv = r.stdout.strip()
    r = subprocess.run(["awg", "pubkey"], input=de_priv, capture_output=True, text=True, check=True)
    de_pub = r.stdout.strip()

    with open(AWG0_PUBKEY, encoding="utf-8") as f:
        ru_pub = f.read().strip()

    # 5. Build DE config
    de_conf = build_de_conf(AWG0_CONF, de_priv, ru_pub, de_iface)

    # 6. Upload
    ssh_exec(ip, port, user, password,
             "mkdir -p /etc/amnezia/amneziawg && chmod 700 /etc/amnezia/amneziawg")
    files = {
        "/tmp/_de_awg0.conf":     de_conf,
        "/tmp/_de_private.key":   de_priv + "\n",
        "/tmp/_de_public.key":    de_pub + "\n",
    }
    try:
        for lp, content in files.items():
            with open(lp, "w") as f: f.write(content)
        for lp in files:
            rp = "/etc/amnezia/amneziawg/" + os.path.basename(lp).replace("_de_", "")
            ok, _, err = ssh_upload(ip, port, user, password, lp, rp)
            if not ok:
                return False, f"Ошибка загрузки {rp}: {err}"
        ssh_exec(ip, port, user, password,
                 "chmod 600 /etc/amnezia/amneziawg/awg0.conf /etc/amnezia/amneziawg/private.key && "
                 "chmod 644 /etc/amnezia/amneziawg/public.key")
    finally:
        for lp in files:
            try: os.remove(lp)
            except: pass

    # 7. Start tunnel on new DE
    ssh_exec(ip, port, user, password,
             "systemctl daemon-reload && systemctl enable awg-quick@awg0 2>/dev/null; "
             "systemctl restart awg-quick@awg0")

    # 8. Backup RU config
    os.makedirs("/var/lib/amnezia-panel", exist_ok=True)
    backup = "/var/lib/amnezia-panel/awg0.conf.before_switch"
    shutil.copy(AWG0_CONF, backup)

    # 9. Modify + restart RU (with rollback on error)
    try:
        with open(AWG0_CONF, encoding="utf-8") as f:
            ru_conf = f.read()
        ru_conf_new = update_ru_peer(ru_conf, de_pub, ip)
        with open(AWG0_CONF, "w", encoding="utf-8") as f:
            f.write(ru_conf_new)
        subprocess.run(["systemctl", "restart", "awg-quick@awg0"],
                       capture_output=True, timeout=30)
    except Exception as e:
        shutil.copy(backup, AWG0_CONF)
        subprocess.run(["systemctl", "restart", "awg-quick@awg0"], capture_output=True)
        return False, f"Ошибка обновления конфига RU, откат выполнен: {e}"

    # 10. Wait for handshake
    for _ in range(6):
        time.sleep(3)
        r = subprocess.run(["ping", "-c", "2", "-W", "2", "10.10.10.1"],
                           capture_output=True, text=True)
        if r.returncode == 0 and " 0% packet loss" in r.stdout:
            data = load_servers()
            data["active_id"] = server["id"]
            save_servers(data)
            return True, f"Успешно переключено на {name} ({ip}). Туннель работает."

    # 11. Rollback
    shutil.copy(backup, AWG0_CONF)
    subprocess.run(["systemctl", "restart", "awg-quick@awg0"], capture_output=True)
    return False, f"Туннель с {ip} не отвечает. Откат на предыдущий конфиг выполнен."

# ================ Traffic / clients ================
def parse_clients():
    if not os.path.exists(AWG_CONF):
        return []
    with open(AWG_CONF, encoding="utf-8") as f:
        content = f.read()
    out = []
    for block in re.split(r"\n(?=\[Peer\])", content):
        if "[Peer]" not in block:
            continue
        nm = re.search(r"#\s*Client:\s*(\S+)", block)
        pk = re.search(r"PublicKey\s*=\s*(\S+)", block)
        ip = re.search(r"AllowedIPs\s*=\s*([0-9.]+)", block)
        if nm and pk:
            out.append({"name": nm.group(1), "pubkey": pk.group(1),
                        "ip": ip.group(1) if ip else "?"})
    return out

def get_live_status():
    try:
        r = subprocess.run(["awg", "show", AWG_IFACE], capture_output=True, text=True, timeout=5)
        stats = {}
        cur = None
        for line in r.stdout.splitlines():
            s = line.strip()
            m = re.match(r"^peer:\s*(\S+)", s)
            if m:
                cur = m.group(1)
                stats[cur] = "OFFLINE"
                continue
            if cur and "latest handshake:" in s:
                if "second" in s or ("minute" in s and "minutes" not in s):
                    stats[cur] = "ONLINE"
                elif "minutes" in s:
                    try:
                        mins = int(re.search(r"(\d+)\s+minutes", s).group(1))
                        if mins < 3:
                            stats[cur] = "ONLINE"
                    except Exception:
                        pass
        return stats
    except Exception:
        return {}

def fmt_handshake(ts):
    if ts == 0: return "никогда"
    return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')

def fmt_bytes(b):
    if b >= 1024**3: return f"{b/1024**3:.2f} GiB"
    if b >= 1024**2: return f"{b/1024**2:.2f} MiB"
    if b >= 1024:    return f"{b/1024:.2f} KiB"
    return f"{int(b)} B"

def enrich(clients):
    live_status = get_live_status()
    db_stats = {}
    if os.path.exists(TRAFFIC_DB):
        try:
            conn = sqlite3.connect(TRAFFIC_DB)
            cur = conn.cursor()
            cur.execute("SELECT pubkey, total_rx, total_tx, last_handshake FROM client_stats")
            for row in cur.fetchall():
                db_stats[row[0]] = {"total_rx": row[1], "total_tx": row[2], "last_handshake": row[3]}
            conn.close()
        except Exception:
            pass
    for c in clients:
        st = db_stats.get(c["pubkey"], {})
        c["rx"] = fmt_bytes(st.get("total_rx", 0))
        c["tx"] = fmt_bytes(st.get("total_tx", 0))
        c["handshake"] = fmt_handshake(st.get("last_handshake", 0))
        c["status"] = live_status.get(c["pubkey"], "NEVER" if not st else "OFFLINE")
    return clients

def total_traffic(clients):
    rx = tx = 0
    if os.path.exists(TRAFFIC_DB):
        try:
            conn = sqlite3.connect(TRAFFIC_DB)
            cur = conn.cursor()
            cur.execute("SELECT SUM(total_rx), SUM(total_tx) FROM client_stats")
            row = cur.fetchone()
            if row:
                rx = row[0] or 0
                tx = row[1] or 0
            conn.close()
        except Exception:
            pass
    return fmt_bytes(rx), fmt_bytes(tx)

# ================ HTML ================
LOGIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru" data-theme="light"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login</title>
<style>
  :root{--bg:#f2f2f7;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--border:#d2d2d7;--accent:#0071e3;--err-bg:#ffe5e5;--err-text:#a00;--header-bg:#1d1d1f;--header-text:#fff}
  html[data-theme=dark]{--bg:#1c1c1e;--card:#2c2c2e;--text:#f5f5f7;--border:#48484a;--accent:#0a84ff;--err-bg:#3a1a1a;--err-text:#ff6961;--header-bg:#000;--header-text:#fff}
  *{box-sizing:border-box} body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:var(--bg);color:var(--text);min-height:100vh;display:flex;flex-direction:column}
  header{background:var(--header-bg);color:var(--header-text);padding:1em 2em}
  header h1{margin:0;font-size:1.15em;font-weight:600}
  header .sub{font-size:.85em;opacity:.7}
  .wrap{flex:1;display:flex;align-items:center;justify-content:center;padding:2em 1em}
  .card{background:var(--card);border-radius:12px;padding:2em;max-width:400px;width:100%;box-shadow:0 4px 20px rgba(0,0,0,.08)}
  h2{margin:0 0 1.2em;text-align:center;font-size:1.2em}
  input{width:100%;padding:.85em 1em;border:1px solid var(--border);border-radius:8px;font-size:1em;margin-bottom:.9em;background:var(--card);color:var(--text)}
  input:focus{outline:none;border-color:var(--accent)}
  button{width:100%;padding:.9em;border:none;border-radius:8px;background:var(--accent);color:#fff;font-size:1em;font-weight:500;cursor:pointer}
  .error{background:var(--err-bg);color:var(--err-text);padding:.8em;border-radius:8px;margin-bottom:1em;text-align:center;font-size:.9em}
</style></head><body>
<header><h1>🛡️ AmneziaWG Panel</h1><div class="sub">__LABEL__</div></header>
<div class="wrap"><div class="card"><h2>Вход</h2>__ERR__
<form method="post" action="/login">
  <input type="text" name="username" placeholder="Username" required autofocus>
  <input type="password" name="password" placeholder="Password" required>
  <button type="submit">Войти</button>
</form></div></div>
</body></html>"""

PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru" data-theme="light"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AmneziaWG Panel</title>
<style>
  :root{--bg:#f2f2f7;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--border:#ececf0;--input-bg:#fff;--input-border:#d2d2d7;--accent:#0071e3;--accent-hover:#0077ed;--btn-bg:#e8e8ed;--btn-bg-hover:#dcdce1;--btn-danger-bg:#ffe5e5;--btn-danger-text:#c00;--btn-danger-hover:#ffcccc;--ok-bg:#e5f7ec;--ok-text:#006c2b;--ok-border:#009e3d;--err-bg:#ffe5e5;--err-text:#a00;--err-border:#c00;--header-bg:#1d1d1f;--header-text:#fff;--online:#009e3d;--offline:#ff9500;--never:#8e8e93}
  html[data-theme=dark]{--bg:#1c1c1e;--card:#2c2c2e;--text:#f5f5f7;--muted:#98989d;--border:#3a3a3c;--input-bg:#1c1c1e;--input-border:#48484a;--accent:#0a84ff;--accent-hover:#409cff;--btn-bg:#3a3a3c;--btn-bg-hover:#48484a;--btn-danger-bg:#4a1e1e;--btn-danger-text:#ff6961;--btn-danger-hover:#5c2424;--ok-bg:#1a3a24;--ok-text:#6bd68a;--ok-border:#30d158;--err-bg:#3a1a1a;--err-text:#ff6961;--err-border:#ff453a;--header-bg:#000;--header-text:#fff;--online:#30d158;--offline:#ffd60a;--never:#8e8e93}
  *{box-sizing:border-box;transition:background-color .15s,color .15s,border-color .15s}
  body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:var(--bg);color:var(--text)}
  header{background:var(--header-bg);color:var(--header-text);padding:1em 2em;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:1em}
  header h1{margin:0;font-size:1.15em;font-weight:600}
  header .sub{font-size:.85em;opacity:.7}
  .header-actions{display:flex;gap:.5em;align-items:center;flex-wrap:wrap}
  .traffic-summary{display:flex;flex-direction:column;gap:.15em;padding:.4em .9em;margin-right:.5em;border:1px solid rgba(255,255,255,.25);border-radius:8px;font-size:.78em;background:rgba(255,255,255,.05);font-family:SF Mono,Consolas,Menlo,monospace;align-self:center;}
  .traffic-row{display:flex;gap:.5em;align-items:center;white-space:nowrap}
  .traffic-label{opacity:.65;min-width:3.2em}
  .traffic-value{color:#4ade80;font-weight:600}
  .traffic-row:nth-child(2) .traffic-value{color:#60a5fa}
  .btn.ghost{background:transparent;color:var(--header-text);border:1px solid rgba(255,255,255,.25);padding:.5em .9em;font-size:.85em;margin:0}
  .btn.ghost:hover{background:rgba(255,255,255,.1)}
  .container{max-width:1200px;margin:2em auto;padding:0 1em}
  .card{background:var(--card);border-radius:12px;padding:1.5em;margin-bottom:1.2em;box-shadow:0 1px 3px rgba(0,0,0,.06)}
  .card h3{margin:0 0 1em;font-size:1em;font-weight:600}
  input[type=text],input[type=password],input[type=number]{width:100%;padding:.75em;border:1px solid var(--input-border);border-radius:8px;font-size:1em;margin-bottom:.6em;background:var(--input-bg);color:var(--text)}
  button,.btn{padding:.6em 1.2em;border:none;border-radius:8px;background:var(--accent);color:#fff;cursor:pointer;font-size:.9em;text-decoration:none;display:inline-block}
  button:hover{background:var(--accent-hover)}
  button:disabled{opacity:.5;cursor:not-allowed}
  .btn{background:var(--btn-bg);color:var(--text);padding:.4em .8em;font-size:.82em;margin-right:.25em}
  .btn:hover{background:var(--btn-bg-hover)}
  .btn.danger{background:var(--btn-danger-bg);color:var(--btn-danger-text)}
  .btn.danger:hover{background:var(--btn-danger-hover)}
  table{width:100%;border-collapse:collapse}
  th,td{padding:.75em .5em;text-align:left;border-bottom:1px solid var(--border);font-size:.9em}
  th{font-size:.72em;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:600}
  td.s-online{color:var(--online);font-weight:600}
  td.s-offline{color:var(--offline)}
  td.s-never{color:var(--never)}
  td.empty{text-align:center;color:var(--muted);padding:2em}
  .alert{padding:.9em 1.2em;border-radius:8px;margin-bottom:1em;font-size:.92em}
  .alert.ok{background:var(--ok-bg);color:var(--ok-text);border-left:4px solid var(--ok-border)}
  .alert.err{background:var(--err-bg);color:var(--err-text);border-left:4px solid var(--err-border)}
  .refresh-indicator{font-size:.78em;color:var(--muted);text-align:right;margin-bottom:.4em}
  .modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.6);z-index:1000}
  .modal-content{background:var(--card);color:var(--text);padding:2em;border-radius:12px;max-width:800px;width:90%;margin:3em auto;position:relative;max-height:90vh;overflow-y:auto}
  .close{position:absolute;top:.5em;right:1em;font-size:1.8em;cursor:pointer;color:var(--muted);line-height:1}
  .close:hover{color:var(--text)}
  .update-list{margin:1em 0;max-height:200px;overflow-y:auto;text-align:left;font-size:0.9em;border:1px solid var(--border);border-radius:8px;padding:0.5em;background:var(--bg)}
  .update-item{padding:0.5em;border-bottom:1px solid var(--border)}
  .update-item:last-child{border-bottom:none}
  .update-date{color:var(--muted);font-size:0.85em}
  .instructions{background:var(--card);border:1px solid var(--accent);border-radius:8px;padding:1em;margin-top:1em}
  .instructions pre{background:var(--bg);padding:1em;border-radius:6px;overflow-x:auto;font-family:monospace;margin:0.5em 0;border:1px solid var(--border)}
  .modal-actions{display:flex;justify-content:flex-end;gap:1em;margin-top:1.5em}
  .lang-dropdown { position: relative; display: inline-block; }
  .lang-menu { display: none; position: absolute; right: 0; background-color: var(--card); min-width: 130px; box-shadow: 0 8px 16px 0 rgba(0,0,0,0.2); z-index: 1001; border-radius: 8px; border: 1px solid var(--border); overflow: hidden; margin-top: 5px; }
  .lang-menu a { color: var(--text); padding: 10px 16px; text-decoration: none; display: block; cursor: pointer; font-size: 0.9em; }
  .lang-menu a:hover { background-color: var(--btn-bg); }
  .lang-menu.show { display: block; }
  .loader-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.75);z-index:2000;color:#fff;align-items:center;justify-content:center;flex-direction:column;font-size:1.2em}
  .loader-spin{font-size:3em;animation:spin 1s linear infinite}
  @keyframes spin{from{transform:rotate(0)}to{transform:rotate(360deg)}}
  .badge-active{background:var(--ok-bg);color:var(--ok-text);padding:0.2em 0.6em;border-radius:6px;font-size:0.8em;font-weight:600}
</style></head><body>
<header>
  <div><h1>🛡️ AmneziaWG Panel</h1><div class="sub">__LABEL__</div></div>
  <div class="header-actions">
    <div class="traffic-summary" title="Total traffic">
      <div class="traffic-row"><span class="traffic-label">⬇ RX:</span><span class="traffic-value" id="total-rx">__TOTAL_RX__</span></div>
      <div class="traffic-row"><span class="traffic-label">⬆ TX:</span><span class="traffic-value" id="total-tx">__TOTAL_TX__</span></div>
    </div>
    <button id="servers-btn" class="btn ghost" data-i18n="btn_servers">🌐 Серверы</button>
    <button id="check-update-btn" class="btn ghost" data-i18n="btn_check_update">🔄 Проверить обновления</button>
    <button id="restart-btn" class="btn ghost" data-i18n="btn_restart">🔄 Перезапуск</button>
    <div class="lang-dropdown">
      <button id="lang-btn" class="btn ghost" title="Switch language">🌐</button>
      <div class="lang-menu" id="lang-menu">
        <a onclick="setLanguage('ru')">🇷🇺 Русский</a>
        <a onclick="setLanguage('en')">🇬🇧 English</a>
      </div>
    </div>
    <button id="theme-toggle" class="btn ghost">🌙</button>
    <a href="/logout" class="btn ghost" data-i18n="btn_logout">🚪 Выход</a>
  </div>
</header>
<div class="container">
  __MSG__ __ERR__
  <div class="card">
    <h3 data-i18n="create_title">➕ Создать нового клиента</h3>
    <form method="post" action="/create">
      <input type="text" name="name" placeholder="Client name (a-z, 0-9, _, -)" pattern="[a-zA-Z0-9_-]+" required autocomplete="off">
      <button type="submit" data-i18n="btn_create">Создать</button>
    </form>
  </div>
  <div class="card">
    <h3><span data-i18n="clients_title">👥 Клиенты</span> (<span id="clients-count">__COUNT__</span>)</h3>
    <div class="refresh-indicator"><span data-i18n="updated">Обновлено:</span> <span id="last-update">--</span></div>
    <table><thead><tr>
      <th data-i18n="th_name">Имя</th><th data-i18n="th_ip">IP</th><th data-i18n="th_status">Статус</th>
      <th data-i18n="th_rx">RX (всего)</th><th data-i18n="th_tx">TX (всего)</th>
      <th data-i18n="th_handshake">Последний handshake</th><th data-i18n="th_actions">Действия</th>
    </tr></thead>
    <tbody id="clients-tbody">__ROWS__</tbody></table>
  </div>
</div>

<!-- Update modal -->
<div id="update-modal" class="modal"><div class="modal-content">
  <span class="close" onclick="document.getElementById('update-modal').style.display='none'">&times;</span>
  <h3 data-i18n="update_avail">Доступны обновления</h3>
  <div id="update-list" class="update-list"></div>
  
  <div class="instructions">
    <p style="margin-top:0;font-weight:bold;" data-i18n="update_instructions">Инструкция по обновлению:</p>
    <p style="font-size:0.9em;margin-bottom:0.5em;" data-i18n="update_ssh_step">1. Подключитесь к серверу по SSH:</p>
    <pre>ssh root@<span id="panel-ip"></span></pre>
    <p style="font-size:0.9em;margin:1em 0 0.5em 0;" data-i18n="update_cmd_step">2. Выполните команду обновления:</p>
    <pre>sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/daff4ik/amneziawg-cascade/main/deploy.sh)" -- --update</pre>
    <p style="font-size:0.8em;color:var(--muted);margin-bottom:0;" data-i18n="update_note">После завершения скрипта обновите эту страницу.</p>
  </div>

  <div class="modal-actions">
    <button class="btn" onclick="document.getElementById('update-modal').style.display='none'" data-i18n="btn_close">Закрыть</button>
  </div>
</div></div>

<!-- Servers modal -->
<div id="servers-modal" class="modal"><div class="modal-content">
  <span class="close" onclick="closeServersModal()">&times;</span>
  <h3 data-i18n="servers_title">Управление SERVER B</h3>
  <div id="servers-list"></div>
  <div style="border-top:1px solid var(--border); padding-top:1em; margin-top:1em;">
    <h4 data-i18n="servers_add_title" style="margin:0 0 .7em;">Добавить сервер</h4>
    <input type="text" id="add-ip" placeholder="IP нового сервера">
    <input type="number" id="add-port" placeholder="SSH-порт (22)" value="22">
    <input type="text" id="add-user" placeholder="SSH-пользователь (root)" value="root">
    <input type="text" id="add-name" placeholder="Название (опционально)">
    <input type="password" id="add-pass" placeholder="SSH-пароль">
    <button id="add-server-btn" onclick="addServer()" data-i18n="btn_servers_add">Добавить и проверить</button>
    <p style="font-size:.8em;color:var(--muted);margin-top:.5em;" data-i18n="servers_note">
      💡 Пароль используется только для проверки SSH. Он не сохраняется. При переключении его нужно будет ввести снова.
    </p>
  </div>
</div></div>

<div id="global-loader" class="loader-overlay" style="display:none;">
  <div class="loader-spin">⚙️</div>
  <p id="loader-text" style="margin-top:1em"></p>
</div>

<script>
var latestShaToSave = "";
var currentLang = localStorage.getItem('lang') || 'ru';
document.getElementById('panel-ip').textContent = window.location.hostname;

const i18n = {
  ru: {
    signin: "Вход", btn_login: "Войти", btn_restart: "🔄 Перезапуск", btn_logout: "🚪 Выход",
    create_title: "➕ Создать нового клиента", btn_create: "Создать",
    clients_title: "👥 Клиенты", updated: "Обновлено:",
    th_name: "Имя", th_ip: "IP", th_status: "Статус", th_rx: "RX (всего)", th_tx: "TX (всего)", th_handshake: "Последний handshake", th_actions: "Действия",
    update_avail: "Доступны обновления", btn_close: "Закрыть",
    update_instructions: "Инструкция по обновлению:",
    update_ssh_step: "1. Подключитесь к серверу по SSH:",
    update_cmd_step: "2. Выполните команду обновления:",
    update_note: "После завершения скрипта обновите эту страницу.",
    no_updates: "Обновлений не найдено. У вас последняя версия.", err_check: "Ошибка проверки обновлений: ",
    btn_check_update: "🔄 Проверить обновления", checking: "⏳ Проверка...",
    btn_servers: "🌐 Серверы", servers_title: "Управление SERVER B", servers_add_title: "Добавить сервер", btn_servers_add: "Добавить и проверить",
    servers_note: "💡 Пароль используется только для проверки SSH. Он не сохраняется. При переключении его нужно будет ввести снова.",
    servers_empty: "Нет добавленных серверов"
  },
  en: {
    signin: "Sign in", btn_login: "Sign in", btn_restart: "🔄 Restart", btn_logout: "🚪 Logout",
    create_title: "➕ Create new client", btn_create: "Create",
    clients_title: "👥 Clients", updated: "Updated:",
    th_name: "Name", th_ip: "IP", th_status: "Status", th_rx: "RX (total)", th_tx: "TX (total)", th_handshake: "Last handshake", th_actions: "Actions",
    update_avail: "Updates Available", btn_close: "Close",
    update_instructions: "Update instructions:",
    update_ssh_step: "1. Connect to the server via SSH:",
    update_cmd_step: "2. Run the update command:",
    update_note: "After the script finishes, refresh this page.",
    no_updates: "No updates found. You are on the latest version.", err_check: "Update check error: ",
    btn_check_update: "🔄 Check Update", checking: "⏳ Checking...",
    btn_servers: "🌐 Servers", servers_title: "SERVER B Management", servers_add_title: "Add server", btn_servers_add: "Add & check",
    servers_note: "💡 Password is used only for SSH check. It is not stored. You will need to enter it again to switch.",
    servers_empty: "No servers added yet"
  }
};

function t(key) { return i18n[currentLang][key] || key; }
function esc(s){ var d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

window.setLanguage = function(lang) {
  currentLang = lang;
  localStorage.setItem('lang', currentLang);
  document.getElementById('lang-menu').classList.remove('show');
  applyLang();
}

document.getElementById('lang-btn').addEventListener('click', function(e) {
  e.stopPropagation();
  document.getElementById('lang-menu').classList.toggle('show');
});
window.addEventListener('click', function() {
  var menu = document.getElementById('lang-menu');
  if (menu && menu.classList.contains('show')) menu.classList.remove('show');
});

function applyLang() {
  document.querySelectorAll('[data-i18n]').forEach(el => {
    var k = el.getAttribute('data-i18n');
    if (i18n[currentLang][k]) el.textContent = i18n[currentLang][k];
  });
  var ub = document.getElementById('check-update-btn');
  if (ub && !ub.disabled) ub.textContent = t('btn_check_update');
  if (typeof refreshTableOnly === 'function') refreshTableOnly();
}

function showLoader(txt) { document.getElementById('loader-text').textContent = txt; document.getElementById('global-loader').style.display = 'flex'; }
function hideLoader() { document.getElementById('global-loader').style.display = 'none'; }

(function(){
  var saved = localStorage.getItem('theme') || 'light';
  document.documentElement.setAttribute('data-theme', saved);
  var tt = document.getElementById('theme-toggle');
  tt.textContent = saved === 'dark' ? '☀️' : '🌙';
  tt.addEventListener('click', function(){
    var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    tt.textContent = next === 'dark' ? '☀️' : '🌙';
  });

  document.getElementById('check-update-btn').addEventListener('click', async function(){
    this.disabled = true;
    this.textContent = t('checking');
    try {
      var r = await fetch('/api/check_update', {credentials: 'same-origin'});
      if (r.status === 401) { location.href = '/login'; return; }
      var d = await r.json();
      if (d.error) { alert(t('err_check') + d.error); }
      else if (!d.has_updates) { alert(t('no_updates')); }
      else {
        latestShaToSave = d.latest_sha;
        var listDiv = document.getElementById('update-list');
        listDiv.innerHTML = d.commits.map(c =>
          '<div class="update-item"><div class="update-date">' + new Date(c.date).toLocaleString() + '</div>' +
          c.message.replace(/\\n/g, '<br>') + '</div>').join('');
        document.getElementById('update-modal').style.display = 'block';
      }
    } catch(e) { alert(t('err_check') + e); }
    finally { this.disabled = false; this.textContent = t('btn_check_update'); }
  });

  document.getElementById('restart-btn').addEventListener('click', function(){
    if (!confirm('Restart awg0 and awg1?')) return;
    var f = document.createElement('form'); f.method='POST'; f.action='/restart';
    document.body.appendChild(f); f.submit();
  });

  document.getElementById('servers-btn').addEventListener('click', async function(){
    document.getElementById('servers-modal').style.display = 'block';
    await loadServers();
  });

  applyLang();
})();

function closeServersModal() { document.getElementById('servers-modal').style.display = 'none'; }

async function loadServers() {
  var r = await fetch('/api/servers', {credentials:'same-origin'});
  if (!r.ok) return;
  var d = await r.json();
  var list = document.getElementById('servers-list');
  if (!d.servers || !d.servers.length) {
    list.innerHTML = '<p style="color:var(--muted)">' + t('servers_empty') + '</p>';
    return;
  }
  var html = '<table style="width:100%"><thead><tr><th>Name</th><th>IP</th><th>Status</th><th>Actions</th></tr></thead><tbody>';
  d.servers.forEach(function(s) {
    var isActive = s.id === d.active_id;
    var statusHtml = isActive ? '<span class="badge-active">ACTIVE</span>' : '<span style="color:var(--muted)">idle</span>';
    var actions = isActive
      ? '<span style="color:var(--muted)">текущий</span>'
      : '<button class="btn" onclick="switchServer(' + s.id + ',\'' + esc(s.name) + '\')">Переключить</button>'
        + '<button class="btn danger" onclick="deleteServer(' + s.id + ')">×</button>';
    html += '<tr><td>' + esc(s.name) + '</td><td>' + esc(s.ip) + '</td><td>' + statusHtml + '</td><td>' + actions + '</td></tr>';
  });
  html += '</tbody></table>';
  list.innerHTML = html;
}

async function addServer() {
  var ip = document.getElementById('add-ip').value.trim();
  var port = parseInt(document.getElementById('add-port').value) || 22;
  var user = document.getElementById('add-user').value.trim() || 'root';
  var name = document.getElementById('add-name').value.trim() || ip;
  var pass = document.getElementById('add-pass').value;
  if (!ip || !pass) { alert('IP и пароль обязательны'); return; }
  var btn = document.getElementById('add-server-btn');
  btn.disabled = true; btn.textContent = '⏳ SSH...';
  try {
    var r = await fetch('/api/servers/add', {
      method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ip: ip, port: port, user: user, password: pass, name: name})
    });
    var d = await r.json();
    if (!r.ok) { alert('Ошибка: ' + (d.error || 'unknown')); return; }
    document.getElementById('add-ip').value = '';
    document.getElementById('add-pass').value = '';
    document.getElementById('add-name').value = '';
    await loadServers();
  } catch(e) { alert('Ошибка: ' + e); }
  finally { btn.disabled = false; btn.textContent = t('btn_servers_add'); }
}

async function switchServer(sid, name) {
  if (!confirm('Переключиться на ' + name + '?\n\nПотребуется ввести SSH-пароль.')) return;
  var pass = prompt('SSH-пароль для ' + name + ':');
  if (!pass) return;
  showLoader('Переключение на ' + name + '... Это может занять 1-2 минуты. Не закрывайте страницу.');
  try {
    var r = await fetch('/api/servers/switch/' + sid, {
      method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({password: pass})
    });
    var d = await r.json();
    hideLoader();
    if (!r.ok) { alert('Ошибка: ' + (d.error || 'unknown')); return; }
    alert('Готово! ' + (d.message || ''));
    await loadServers();
  } catch(e) {
    hideLoader();
    alert('Ошибка: ' + e);
  }
}

async function deleteServer(sid) {
  if (!confirm('Удалить сервер из списка? (файлы не трогаем)')) return;
  await fetch('/api/servers/' + sid, {method:'DELETE', credentials:'same-origin'});
  await loadServers();
}

var _tableClients = [];

function buildRow(c){
  var statusLabel = c.status;
  if (currentLang === 'ru') {
    if (statusLabel === 'ONLINE') statusLabel = 'ОНЛАЙН';
    if (statusLabel === 'OFFLINE') statusLabel = 'ОФФЛАЙН';
    if (statusLabel === 'NEVER') statusLabel = 'НИКОГДА';
  }
  var btnDelete = currentLang === 'ru' ? 'Удалить' : 'Delete';
  return '<tr>'+
    '<td><strong>'+esc(c.name)+'</strong></td>'+
    '<td>'+esc(c.ip)+'</td>'+
    '<td class="s-'+c.status.toLowerCase()+'">'+statusLabel+'</td>'+
    '<td>'+esc(c.rx)+'</td>'+
    '<td>'+esc(c.tx)+'</td>'+
    '<td>'+esc(c.handshake)+'</td>'+
    '<td>'+
      '<a href="/download/conf/'+encodeURIComponent(c.name)+'" class="btn">.conf</a>'+
      '<a href="/download/vpn/'+encodeURIComponent(c.name)+'" class="btn">vpn://</a>'+
      '<form method="post" action="/delete/'+encodeURIComponent(c.name)+'" onsubmit="return confirm(\''+(currentLang==='ru'?'Удалить клиента ':'Delete client ')+esc(c.name)+'?\')" style="display:inline">'+
      '<button type="submit" class="btn danger">'+btnDelete+'</button></form>'+
    '</td></tr>';
}

function refreshTableOnly() {
  if (!_tableClients.length) return;
  var tb = document.getElementById('clients-tbody');
  tb.innerHTML = _tableClients.map(buildRow).join('');
}

var updating = false;
async function refresh(){
  if (updating) return;
  updating = true;
  try {
    var r = await fetch('/api/clients', { credentials:'same-origin', cache:'no-store' });
    if (r.status === 401) { location.href = '/login'; return; }
    if (!r.ok) return;
    _tableClients = await r.json();
    var tb = document.getElementById('clients-tbody');
    tb.innerHTML = _tableClients.length === 0
      ? '<tr><td colspan="7" class="empty">'+(currentLang==='ru'?'Нет клиентов':'No clients yet')+'</td></tr>'
      : _tableClients.map(buildRow).join('');
    document.getElementById('clients-count').textContent = _tableClients.length;
    document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
  } catch(e) {} finally { updating = false; }
}
setInterval(refresh, 5000);
setTimeout(refresh, 500);
</script></body></html>"""

def render_login(error=""):
    err = f'<div class="error">{error}</div>' if error else ""
    return LOGIN_TEMPLATE.replace("__ERR__", err).replace("__LABEL__", SERVER_LABEL)

def render_page(clients, message="", error=""):
    rows = []
    for c in clients:
        st = c.get("status", "NEVER")
        cls = st.lower()
        n = c["name"]
        rows.append(
            '<tr>'
            f'<td><strong>{n}</strong></td>'
            f'<td>{c["ip"]}</td>'
            f'<td class="s-{cls}">{st}</td>'
            f'<td>{c.get("rx","0 B")}</td>'
            f'<td>{c.get("tx","0 B")}</td>'
            f'<td>{c.get("handshake","никогда")}</td>'
            '<td>'
            f'<a href="/download/conf/{n}" class="btn">.conf</a>'
            f'<a href="/download/vpn/{n}" class="btn">vpn://</a>'
            f'<form method="post" action="/delete/{n}" onsubmit="return confirm(\'Delete client {n}?\')" style="display:inline">'
            '<button type="submit" class="btn danger">Удалить</button></form>'
            '</td></tr>'
        )
    if not rows:
        rows.append('<tr><td colspan="7" class="empty">No clients yet</td></tr>')
    msg = f'<div class="alert ok">{message}</div>' if message else ""
    err = f'<div class="alert err">{error}</div>' if error else ""
    trx, ttx = total_traffic(clients)
    return (PAGE_TEMPLATE
            .replace("__MSG__", msg)
            .replace("__ERR__", err)
            .replace("__LABEL__", SERVER_LABEL)
            .replace("__COUNT__", str(len(clients)))
            .replace("__ROWS__", "".join(rows))
            .replace("__TOTAL_RX__", trx)
            .replace("__TOTAL_TX__", ttx))

# ================ Routes ================
app = FastAPI(docs_url=None, redoc_url=None)

@app.exception_handler(RedirectToLogin)
async def _rtl(request: Request, exc: RedirectToLogin):
    return RedirectResponse("/login", status_code=303)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    try:
        _, p = load_credentials()
        if hmac.compare_digest(request.cookies.get(SESSION_COOKIE, ""), make_token(p)):
            return RedirectResponse("/", status_code=303)
    except RuntimeError:
        pass
    return render_login(error)

@app.post("/login")
async def login_submit(username: str = Form(...), password: str = Form(...)):
    try:
        u, p = load_credentials()
    except RuntimeError:
        return RedirectResponse("/login?error=config+error", status_code=303)
    if not (hmac.compare_digest(username, u) and hmac.compare_digest(password, p)):
        return RedirectResponse("/login?error=Invalid+credentials", status_code=303)
    r = RedirectResponse("/", status_code=303)
    r.set_cookie(SESSION_COOKIE, make_token(p), max_age=SESSION_DAYS*86400,
                 httponly=True, secure=True, samesite="lax", path="/")
    return r

@app.get("/logout")
async def logout():
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(SESSION_COOKIE, path="/")
    return r

def _notify(msg):
    try:
        if os.path.exists(NOTIFY_SCRIPT):
            subprocess.run([NOTIFY_SCRIPT, msg], timeout=10, capture_output=True)
    except Exception:
        pass

@app.on_event("startup")
async def on_startup():
    ensure_current_server_in_list()

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, message: str = "", error: str = ""):
    current_user(request)
    return render_page(enrich(parse_clients()), message, error)

@app.get("/api/clients")
async def api_clients(request: Request):
    current_user(request)
    return JSONResponse(enrich(parse_clients()))

@app.get("/api/check_update")
async def api_check_update(request: Request):
    current_user(request)
    result = await run_in_threadpool(check_github_updates)
    return JSONResponse(result)

# ================ Servers API ================
@app.get("/api/servers")
async def api_servers_list(request: Request):
    current_user(request)
    data = load_servers()
    return {"active_id": data.get("active_id"), "servers": data.get("servers", [])}

def _add_server_sync(body):
    ip = body.get("ip", "").strip()
    port = int(body.get("port", 22))
    user = body.get("user", "root").strip()
    password = body.get("password", "")
    name = body.get("name", "").strip() or ip
    if not ip or not password:
        return {"error": "IP и пароль обязательны"}
    ok, out, err = ssh_exec(ip, port, user, password, "echo SSH_OK", timeout=20)
    if not ok or "SSH_OK" not in (out or ""):
        return {"error": f"SSH: {err or out or 'timeout'}"}
    data = load_servers()
    for s in data["servers"]:
        if s["ip"] == ip and s["port"] == port:
            return {"error": "Такой сервер уже добавлен"}
    new_id = max([s["id"] for s in data["servers"]] or [0]) + 1
    server = {"id": new_id, "ip": ip, "port": port, "user": user, "name": name}
    data["servers"].append(server)
    save_servers(data)
    return {"ok": True, "server": server}

@app.post("/api/servers/add")
async def api_servers_add(request: Request):
    current_user(request)
    body = await request.json()
    result = await run_in_threadpool(_add_server_sync, body)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result

@app.delete("/api/servers/{sid}")
async def api_servers_del(request: Request, sid: int):
    current_user(request)
    data = load_servers()
    if data.get("active_id") == sid:
        return JSONResponse({"error": "Нельзя удалить активный сервер"}, status_code=400)
    data["servers"] = [s for s in data["servers"] if s["id"] != sid]
    save_servers(data)
    return {"ok": True}

@app.post("/api/servers/switch/{sid}")
async def api_servers_switch(request: Request, sid: int):
    current_user(request)
    try:
        body = await request.json()
        password = body.get("password", "")
    except Exception:
        password = ""
    if not password:
        return JSONResponse({"error": "Требуется SSH-пароль"}, status_code=400)
    data = load_servers()
    server = next((s for s in data["servers"] if s["id"] == sid), None)
    if not server:
        return JSONResponse({"error": "Сервер не найден"}, status_code=404)
    if data.get("active_id") == sid:
        return JSONResponse({"error": "Этот сервер уже активен"}, status_code=400)
    ok, msg = await run_in_threadpool(do_switch_server, server, password)
    if ok:
        _notify(f"🌐 <b>Переключение SERVER-B</b>\nНа: <code>{server['ip']}</code> ({server['name']})\nРезультат: OK")
        return {"ok": True, "message": msg}
    else:
        _notify(f"❌ <b>Ошибка переключения SERVER-B</b>\nНа: <code>{server['ip']}</code>\n{msg[:300]}")
        return JSONResponse({"error": msg}, status_code=500)

# ================ Clients API ================
@app.post("/create")
async def create_client(request: Request, name: str = Form(...)):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    if not n:
        return RedirectResponse("/?error=Invalid+name", status_code=303)
    try:
        subprocess.run([ADD_SCRIPT, n], input="y\n", capture_output=True, text=True, timeout=90, check=True)
        _notify(f"👤 <b>Новый клиент</b> (через панель)\nИмя: <code>{n}</code>")
        return RedirectResponse(f"/?message=Client+{n}+created", status_code=303)
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "error").strip()[:200]
        return RedirectResponse(f"/?error={err.replace(' ','+').replace(chr(10),' ')}", status_code=303)
    except subprocess.TimeoutExpired:
        return RedirectResponse("/?error=Timeout", status_code=303)

@app.post("/delete/{name}")
async def delete_client(request: Request, name: str):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    try:
        subprocess.run([REMOVE_SCRIPT, n], input="y\n", capture_output=True, text=True, timeout=60, check=True)
        _notify(f"🗑️ <b>Клиент удалён</b> (через панель)\nИмя: <code>{n}</code>")
        return RedirectResponse(f"/?message=Client+{n}+deleted", status_code=303)
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "error").strip()[:200]
        return RedirectResponse(f"/?error={err.replace(' ','+').replace(chr(10),' ')}", status_code=303)

@app.post("/restart")
async def restart_tunnels(request: Request):
    current_user(request)
    res = []
    for iface in ("awg0", "awg1"):
        try:
            subprocess.run(["systemctl", "restart", f"awg-quick@{iface}"],
                           capture_output=True, timeout=30, check=True)
            res.append(f"{iface}: OK")
        except Exception:
            res.append(f"{iface}: FAIL")
    msg = " | ".join(res)
    _notify(f"🔄 <b>Перезапуск</b>\n{msg}")
    return RedirectResponse(f"/?message={msg.replace(' ','+')}", status_code=303)

@app.get("/download/conf/{name}")
async def dl_conf(request: Request, name: str):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    p = os.path.join(CLIENTS_DIR, f"{n}.conf")
    if not os.path.isfile(p): raise HTTPException(404, "not found")
    return FileResponse(p, media_type="text/plain", filename=f"{n}.conf")

@app.get("/download/vpn/{name}")
async def dl_vpn(request: Request, name: str):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    p = os.path.join(CLIENTS_DIR, f"{n}.vpn.txt")
    if not os.path.isfile(p): raise HTTPException(404, "not found")
    return FileResponse(p, media_type="text/plain", filename=f"{n}.vpn.txt")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info",
                access_log=False, server_header=False,
                ssl_keyfile="/etc/amnezia-panel-ssl/key.pem",
                ssl_certfile="/etc/amnezia-panel-ssl/cert.pem")