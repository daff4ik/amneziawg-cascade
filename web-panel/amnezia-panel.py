#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AmneziaWG Web Panel — Optimized, Mobile-First, Secure"""
import os
import re
import hmac
import hashlib
import sqlite3
import subprocess
import time
import urllib.request
import json
import datetime
import shutil
from fastapi import FastAPI, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse
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

# ================ Конфигурация запуска ================
def get_config_val(key, default):
    if not os.path.exists(CONFIG_FILE): return default
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith(f"{key}="):
                return line.strip().split("=", 1)[1]
    return default

BASE_URL = get_config_val("base_url", "")
BIND_HOST = get_config_val("host", "0.0.0.0")
USE_SSL = get_config_val("ssl", "true").lower() == "true"

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
        if request.url.path.startswith("/api/") or request.url.path.startswith("/download/"):
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
        if not ok: return False, "", err
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
        if v: params[p] = v
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

    ok, out, err = ssh_exec(ip, port, user, password, "echo SSH_OK")
    if not ok or "SSH_OK" not in (out or ""):
        return False, f"SSH до {ip} не работает: {err or out}"

    ok, out, err = install_awg_remote(ip, port, user, password)
    if not ok or "INSTALLED_OK" not in (out or ""):
        return False, f"Установка AmneziaWG не удалась: {(err or out)[-300:]}"

    ok, out, _ = ssh_exec(ip, port, user, password,
                          "ip route get 8.8.8.8 | awk '{print $5}' | head -1")
    de_iface = (out or "").strip().split("\n")[0]
    if not de_iface:
        return False, "Не удалось определить внешний интерфейс на новом DE"

    r = subprocess.run(["awg", "genkey"], capture_output=True, text=True, check=True)
    de_priv = r.stdout.strip()
    r = subprocess.run(["awg", "pubkey"], input=de_priv, capture_output=True, text=True, check=True)
    de_pub = r.stdout.strip()

    with open(AWG0_PUBKEY, encoding="utf-8") as f:
        ru_pub = f.read().strip()

    de_conf = build_de_conf(AWG0_CONF, de_priv, ru_pub, de_iface)

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
            if not ok: return False, f"Ошибка загрузки {rp}: {err}"
        ssh_exec(ip, port, user, password,
                 "chmod 600 /etc/amnezia/amneziawg/awg0.conf /etc/amnezia/amneziawg/private.key && "
                 "chmod 644 /etc/amnezia/amneziawg/public.key")
    finally:
        for lp in files:
            try: os.remove(lp)
            except: pass

    ssh_exec(ip, port, user, password,
             "systemctl daemon-reload && systemctl enable awg-quick@awg0 2>/dev/null; "
             "systemctl restart awg-quick@awg0")

    os.makedirs("/var/lib/amnezia-panel", exist_ok=True)
    backup = "/var/lib/amnezia-panel/awg0.conf.before_switch"
    shutil.copy(AWG0_CONF, backup)

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

    for _ in range(6):
        time.sleep(3)
        r = subprocess.run(["ping", "-c", "2", "-W", "2", "10.10.10.1"],
                           capture_output=True, text=True)
        if r.returncode == 0 and " 0% packet loss" in r.stdout:
            data = load_servers()
            data["active_id"] = server["id"]
            save_servers(data)
            return True, f"Успешно переключено на {name} ({ip}). Туннель работает."

    shutil.copy(backup, AWG0_CONF)
    subprocess.run(["systemctl", "restart", "awg-quick@awg0"], capture_output=True)
    return False, f"Туннель с {ip} не отвечает. Откат на предыдущий конфиг выполнен."

# ================ Traffic / clients ================
def parse_clients():
    if not os.path.exists(AWG_CONF): return []
    with open(AWG_CONF, encoding="utf-8") as f: content = f.read()
    out = []
    for block in re.split(r"\n(?=\[Peer\])", content):
        if "[Peer]" not in block: continue
        nm = re.search(r"#\s*Client:\s*(\S+)", block)
        pk = re.search(r"PublicKey\s*=\s*(\S+)", block)
        ip = re.search(r"AllowedIPs\s*=\s*([0-9.]+)", block)
        if nm and pk:
            out.append({"name": nm.group(1), "pubkey": pk.group(1), "ip": ip.group(1) if ip else "?"})
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
                        if mins < 3: stats[cur] = "ONLINE"
                    except: pass
        return stats
    except: return {}

def fmt_handshake(ts):
    if ts == 0: return "никогда"
    return datetime.datetime.fromtimestamp(ts).strftime('%d.%m.%Y %H:%M:%S')

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
        except: pass
    for c in clients:
        st = db_stats.get(c["pubkey"], {})
        c["rx_bytes"] = st.get("total_rx", 0)
        c["tx_bytes"] = st.get("total_tx", 0)
        c["handshake_ts"] = st.get("last_handshake", 0)
        c["rx"] = fmt_bytes(c["rx_bytes"])
        c["tx"] = fmt_bytes(c["tx_bytes"])
        c["handshake"] = fmt_handshake(c["handshake_ts"])
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
                rx, tx = row[0] or 0, row[1] or 0
            conn.close()
        except: pass
    return fmt_bytes(rx), fmt_bytes(tx)

# ================ HTML Templates ================

LOGIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>AmneziaWG Panel - Login</title>
<style>
  :root { --bg: #f2f2f7; --surface: #ffffff; --text: #1d1d1f; --text-muted: #86868b; --border: #d2d2d7; --primary: #0071e3; --primary-hover: #0077ed; --danger-bg: #ffe5e5; --danger-text: #d93025; --header-bg: #1d1d1f; --header-text: #ffffff; }
  html[data-theme="dark"] { --bg: #1c1c1e; --surface: #2c2c2e; --text: #f5f5f7; --text-muted: #98989d; --border: #48484a; --primary: #0a84ff; --primary-hover: #409cff; --danger-bg: #3a1a1a; --danger-text: #ff6961; --header-bg: #000000; }
  * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; transition: background-color 0.2s, border-color 0.2s; }
  body { margin: 0; background: var(--bg); color: var(--text); display: flex; flex-direction: column; min-height: 100vh; }
  header { background: var(--header-bg); color: var(--header-text); padding: 1rem; text-align: center; }
  header h1 { margin: 0; font-size: 1.25rem; font-weight: 600; }
  .auth-container { flex: 1; display: flex; align-items: center; justify-content: center; padding: 1rem; }
  .auth-card { background: var(--surface); padding: 2rem; border-radius: 16px; box-shadow: 0 4px 24px rgba(0,0,0,0.06); width: 100%; max-width: 400px; }
  .auth-card h2 { margin-top: 0; margin-bottom: 1.5rem; text-align: center; font-size: 1.25rem; }
  .input-group { margin-bottom: 1.25rem; }
  input { width: 100%; padding: 0.875rem 1rem; border: 1px solid var(--border); border-radius: 10px; background: var(--bg); color: var(--text); font-size: 1rem; }
  input:focus-visible { outline: 2px solid var(--primary); outline-offset: -1px; border-color: transparent; }
  button { width: 100%; padding: 0.875rem; background: var(--primary); color: #fff; border: none; border-radius: 10px; font-size: 1rem; font-weight: 600; cursor: pointer; }
  button:hover { background: var(--primary-hover); }
  .error-msg { background: var(--danger-bg); color: var(--danger-text); padding: 0.75rem; border-radius: 8px; font-size: 0.875rem; text-align: center; margin-bottom: 1.25rem; }
</style>
</head>
<body>
<header><h1>🛡️ AmneziaWG Panel</h1><div style="font-size: 0.85em; opacity: 0.7;">__LABEL__</div></header>
<div class="auth-container">
  <div class="auth-card">
    <h2>Авторизация</h2>
    __ERR__
    <form method="post" action="__BASE__/login">
      <div class="input-group"><input type="text" name="username" placeholder="Логин" required autofocus autocomplete="username"></div>
      <div class="input-group"><input type="password" name="password" placeholder="Пароль" required autocomplete="current-password"></div>
      <button type="submit">Войти</button>
    </form>
  </div>
</div>
</body>
</html>"""

PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>AmneziaWG Panel</title>
<style>
  :root { 
    --bg: #f2f2f7; --surface: #ffffff; --text: #1d1d1f; --text-muted: #86868b; --border: #ececf0; 
    --input-bg: #ffffff; --input-border: #d2d2d7; 
    --primary: #0071e3; --primary-hover: #0077ed; 
    --btn-bg: #e8e8ed; --btn-hover: #dcdce1;
    --danger-bg: #ffe5e5; --danger-text: #d93025; --danger-hover: #ffcccc;
    --success-bg: #e5f7ec; --success-text: #006c2b; --success-border: #009e3d;
    --header-bg: #1d1d1f; --header-text: #ffffff; 
    --online: #34c759; --offline: #ff9500; --never: #8e8e93;
    --font-stack: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  html[data-theme="dark"] { 
    --bg: #000000; --surface: #1c1c1e; --text: #f5f5f7; --text-muted: #98989d; --border: #38383a; 
    --input-bg: #1c1c1e; --input-border: #48484a; 
    --primary: #0a84ff; --primary-hover: #409cff; 
    --btn-bg: #2c2c2e; --btn-hover: #3a3a3c;
    --danger-bg: #3a1a1a; --danger-text: #ff6961; --danger-hover: #4a1e1e;
    --success-bg: #1a3a24; --success-text: #6bd68a; --success-border: #30d158;
    --header-bg: #000000; --header-border: #333333;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font-family: var(--font-stack); padding-bottom: 2rem; }
  a { color: var(--primary); text-decoration: none; }
  *:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }

  header { background: var(--header-bg); border-bottom: 1px solid var(--header-border, transparent); padding: 1rem; position: sticky; top: 0; z-index: 100; }
  .header-content { max-width: 1200px; margin: 0 auto; display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 1rem; }
  .header-brand { display: flex; flex-direction: column; }
  .header-brand h1 { margin: 0; font-size: 1.25rem; font-weight: 600; color: var(--header-text); }
  .header-brand .sub { font-size: 0.85rem; opacity: 0.7; color: var(--header-text); }
  
  .header-controls { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  .traffic-summary { background: rgba(255,255,255,0.1); padding: 0.3rem 0.6rem; border-radius: 8px; font-family: monospace; font-size: 0.8rem; color: var(--header-text); line-height: 1.4; border: 1px solid rgba(255,255,255,0.2); }
  .traffic-summary span { font-weight: bold; }
  .rx-color { color: #6bd68a; } .tx-color { color: #60a5fa; }
  
  .btn-ghost { background: transparent; color: var(--header-text); border: 1px solid rgba(255,255,255,0.3); padding: 0.4rem 0.8rem; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }
  .btn-ghost:hover { background: rgba(255,255,255,0.1); }
  
  .tabs { display: flex; gap: 0.5rem; margin: 1.5rem auto 1rem; max-width: 1200px; padding: 0 1rem; overflow-x: auto; }
  .tab { padding: 0.6rem 1.2rem; background: transparent; border: none; font-size: 1rem; font-weight: 600; color: var(--text-muted); cursor: pointer; border-bottom: 2px solid transparent; transition: 0.2s; white-space: nowrap; }
  .tab.active { color: var(--primary); border-bottom-color: var(--primary); }
  .tab:hover:not(.active) { color: var(--text); }
  
  .container { max-width: 1200px; margin: 0 auto; padding: 0 1rem; }
  .view-section { display: none; animation: fadeIn 0.2s ease-in-out; }
  .view-section.active { display: block; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }

  .card { background: var(--surface); border-radius: 16px; padding: 1.5rem; margin-bottom: 1.25rem; box-shadow: 0 2px 12px rgba(0,0,0,0.04); border: 1px solid var(--border); }
  .card h3 { margin: 0 0 1rem; font-size: 1.1rem; font-weight: 600; }
  
  .form-group { display: flex; gap: 0.5rem; flex-wrap: wrap; }
  input[type="text"], input[type="number"], input[type="password"] { 
    flex: 1; min-width: 200px; padding: 0.75rem 1rem; border: 1px solid var(--input-border); border-radius: 10px; background: var(--input-bg); color: var(--text); font-size: 1rem; 
  }
  .btn { display: inline-flex; align-items: center; justify-content: center; padding: 0.75rem 1.2rem; border: none; border-radius: 10px; background: var(--btn-bg); color: var(--text); font-size: 0.9rem; font-weight: 500; cursor: pointer; text-decoration: none; min-height: 44px; }
  .btn:hover { background: var(--btn-hover); }
  .btn-primary { background: var(--primary); color: #ffffff; }
  .btn-primary:hover { background: var(--primary-hover); color: #ffffff; }
  .btn-danger { background: var(--danger-bg); color: var(--danger-text); }
  .btn-danger:hover { background: var(--danger-hover); }
  .btn-small { padding: 0.4rem 0.8rem; font-size: 0.85rem; min-height: 32px; border-radius: 6px; }

  /* Стили переключателя (Toggle Switch) */
  .switch { position: relative; display: inline-block; width: 44px; height: 24px; flex-shrink: 0; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: var(--border); transition: .3s; border-radius: 24px; }
  .slider:before { position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; transition: .3s; border-radius: 50%; box-shadow: 0 1px 3px rgba(0,0,0,0.3); }
  input:checked + .slider { background-color: var(--success-border); }
  input:checked + .slider:before { transform: translateX(20px); }

  .toolbar { display: flex; flex-wrap: wrap; gap: 1rem; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
  .search-box { flex: 1; min-width: 250px; position: relative; }
  .search-box input { width: 100%; padding-left: 2.5rem; border-radius: 10px; }
  .search-box::before { content: "🔍"; position: absolute; left: 0.8rem; top: 50%; transform: translateY(-50%); font-size: 0.9rem; opacity: 0.6; }
  .filter-box select { padding: 0.75rem 1rem; border-radius: 10px; border: 1px solid var(--input-border); background: var(--input-bg); color: var(--text); font-size: 0.95rem; cursor: pointer; min-height: 44px;}

  .table-wrapper { overflow-x: auto; width: 100%; }
  table { width: 100%; border-collapse: collapse; text-align: left; }
  th, td { padding: 0.875rem; border-bottom: 1px solid var(--border); font-size: 0.9rem; }
  th { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); font-weight: 600; cursor: pointer; user-select: none; }
  th:hover { color: var(--text); }
  th.sort-asc::after { content: " \2191"; color: var(--primary); }
  th.sort-desc::after { content: " \2193"; color: var(--primary); }
  
  .status-badge { display: inline-block; padding: 0.25rem 0.5rem; border-radius: 6px; font-size: 0.75rem; font-weight: 600; letter-spacing: 0.03em; }
  .status-online { background: rgba(52, 199, 89, 0.15); color: var(--online); }
  .status-offline { background: rgba(255, 149, 0, 0.15); color: var(--offline); }
  .status-never { background: rgba(142, 142, 147, 0.15); color: var(--never); }

  .actions-cell { display: flex; gap: 0.4rem; flex-wrap: wrap; }
  .empty-state { text-align: center; padding: 3rem 1rem; color: var(--text-muted); }

  @media (max-width: 768px) {
    .header-content { flex-direction: column; align-items: stretch; text-align: center; }
    .header-controls { justify-content: center; }
    .table-wrapper table, .table-wrapper tbody, .table-wrapper tr { display: block; width: 100%; }
    .table-wrapper thead { display: none; }
    .table-wrapper tr { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 1rem; padding: 1rem; display: flex; flex-direction: column; gap: 0.5rem; }
    .table-wrapper td { display: flex; justify-content: space-between; align-items: center; padding: 0.25rem 0; border: none; font-size: 0.95rem; text-align: right; }
    .table-wrapper td::before { content: attr(data-label); color: var(--text-muted); font-size: 0.8rem; text-transform: uppercase; font-weight: 600; text-align: left; margin-right: 1rem; }
    .table-wrapper td:last-child { margin-top: 0.5rem; padding-top: 0.75rem; border-top: 1px dashed var(--border); justify-content: flex-end; width: 100%; }
    .actions-cell { justify-content: flex-end; width: 100%; }
    .toolbar { flex-direction: column; align-items: stretch; }
  }

  .modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 2000; align-items: center; justify-content: center; padding: 1rem; backdrop-filter: blur(2px); }
  .modal-content { background: var(--surface); color: var(--text); padding: 1.5rem; border-radius: 16px; width: 100%; max-width: 600px; max-height: 90vh; overflow-y: auto; position: relative; box-shadow: 0 10px 30px rgba(0,0,0,0.2); }
  .modal-close { position: absolute; top: 1rem; right: 1rem; font-size: 1.5rem; cursor: pointer; color: var(--text-muted); background: none; border: none; line-height: 1; padding: 0.5rem; }
  .modal-close:hover { color: var(--text); }
  
  .global-loader { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.8); z-index: 3000; color: #fff; flex-direction: column; align-items: center; justify-content: center; }
  .spinner { width: 40px; height: 40px; border: 4px solid rgba(255,255,255,0.3); border-radius: 50%; border-top-color: var(--primary); animation: spin 1s linear infinite; margin-bottom: 1rem; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .update-list { margin: 1rem 0; max-height: 200px; overflow-y: auto; font-size: 0.9em; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 0.5rem; }
  .update-item { padding: 0.5rem; border-bottom: 1px solid var(--border); }
  .update-item:last-child { border: none; }
  .alert { padding: 1rem; border-radius: 10px; margin-bottom: 1rem; font-size: 0.95rem; font-weight: 500; display: flex; align-items: center; gap: 0.5rem; }
  .alert.ok { background: var(--success-bg); color: var(--success-text); border: 1px solid var(--success-border); }
  .alert.err { background: var(--danger-bg); color: var(--danger-text); border: 1px solid var(--danger-text); }
</style>
</head>
<body>
<header>
  <div class="header-content">
    <div class="header-brand">
      <h1>🛡️ AmneziaWG Panel</h1>
      <div class="sub">__LABEL__</div>
    </div>
    <div class="header-controls">
      <div class="traffic-summary" title="Суммарный трафик">
        <div>⬇ <span class="rx-color" id="total-rx">__TOTAL_RX__</span></div>
        <div>⬆ <span class="tx-color" id="total-tx">__TOTAL_TX__</span></div>
      </div>
      <button class="btn-ghost" onclick="checkUpdates()">🔄 Обновления</button>
      <button class="btn-ghost" onclick="restartTunnels()">⚡ Рестарт</button>
      <button class="btn-ghost" onclick="toggleTheme()" id="theme-btn">🌙</button>
      <a href="__BASE__/logout" class="btn-ghost">🚪 Выход</a>
    </div>
  </div>
</header>

<div class="tabs">
  <button class="tab active" onclick="switchTab('clients')">👥 Клиенты (<span id="clients-count">__COUNT__</span>)</button>
  <button class="tab" onclick="switchTab('servers')">🌐 Серверы</button>
</div>

<div class="container">
  __MSG__
  __ERR__

  <div id="clients-view" class="view-section active">
    
    <div class="card" style="display: flex; gap: 1rem; flex-wrap: wrap;">
      <div style="flex: 1; min-width: 280px;">
        <h3 style="margin-top:0;">➕ Новый клиент</h3>
        <form class="form-group" method="post" action="__BASE__/create" onsubmit="showLoader('Создание клиента...');" style="margin-bottom:0;">
          <input type="text" name="name" placeholder="Имя (латиница, цифры, - _)" pattern="[a-zA-Z0-9_-]+" required autocomplete="off">
          <button type="submit" class="btn btn-primary">Создать</button>
        </form>
      </div>
      <div style="border-left: 1px solid var(--border); padding-left: 1rem; min-width: 250px; display: flex; align-items: center; justify-content: space-between; gap: 1rem;">
        <div>
          <div style="font-weight: 600; font-size: 1rem; margin-bottom: 0.25rem;">Внутрисетевая связь (LAN)</div>
          <div style="font-size: 0.85rem; color: var(--text-muted); line-height: 1.3;">Позволяет VPN-клиентам<br>взаимодействовать друг с другом</div>
        </div>
        <label class="switch">
          <input type="checkbox" id="lan-toggle" onchange="toggleLanStatus()">
          <span class="slider"></span>
        </label>
      </div>
    </div>

    <div class="card">
      <div class="toolbar">
        <div class="search-box">
          <input type="text" id="search-input" placeholder="Поиск по имени или IP..." oninput="applyFilters()">
        </div>
        <div class="filter-box">
          <select id="status-filter" onchange="applyFilters()">
            <option value="all">Все статусы</option>
            <option value="ONLINE">Онлайн</option>
            <option value="OFFLINE">Офлайн</option>
          </select>
        </div>
      </div>

      <div class="table-wrapper">
        <table>
          <thead>
            <tr>
              <th data-sort="name" onclick="setSort('name')">Имя</th>
              <th data-sort="ip" onclick="setSort('ip')">IP</th>
              <th data-sort="status" onclick="setSort('status')">Статус</th>
              <th data-sort="rx_bytes" onclick="setSort('rx_bytes')">RX</th>
              <th data-sort="tx_bytes" onclick="setSort('tx_bytes')">TX</th>
              <th data-sort="handshake_ts" onclick="setSort('handshake_ts')">Handshake</th>
              <th style="text-align: right;">Действия</th>
            </tr>
          </thead>
          <tbody id="clients-tbody"></tbody>
        </table>
        <div id="empty-state" class="empty-state" style="display:none;">Нет клиентов</div>
      </div>
      <div style="text-align: right; font-size: 0.75rem; color: var(--text-muted); margin-top: 1rem;">
        Обновлено: <span id="last-update">--:--:--</span>
      </div>
    </div>
  </div>

  <div id="servers-view" class="view-section">
    <div class="card">
      <h3 style="margin-top:0;">Управление выходным шлюзом (SERVER B)</h3>
      <p style="font-size: 0.9rem; color: var(--text-muted); margin-bottom: 1rem;">Здесь можно добавить новый VPS за рубежом и переключить на него весь трафик туннеля.</p>
      <div class="table-wrapper">
        <table id="servers-table">
          <thead><tr><th>Имя</th><th>IP</th><th>Статус</th><th style="text-align: right;">Действия</th></tr></thead>
          <tbody id="servers-tbody"></tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <h4 style="margin-top:0;">Добавить сервер</h4>
      <div class="form-group" style="margin-bottom: 0.5rem;">
        <input type="text" id="add-ip" placeholder="IP адрес">
        <input type="number" id="add-port" placeholder="SSH порт (22)" value="22" style="max-width: 120px;">
        <input type="text" id="add-user" placeholder="Пользователь (root)" value="root" style="max-width: 150px;">
      </div>
      <div class="form-group">
        <input type="password" id="add-pass" placeholder="SSH пароль (не сохраняется)">
        <input type="text" id="add-name" placeholder="Название (опционально)">
        <button class="btn btn-primary" id="add-server-btn" onclick="addServer()">Проверить и добавить</button>
      </div>
    </div>
  </div>
</div>

<!-- Модальное окно: LAN -->
<div id="lan-modal" class="modal">
  <div class="modal-content">
    <button class="modal-close" onclick="closeModal('lan-modal')">&times;</button>
    <h3>🌐 Внутрисетевая связь (LAN) активирована</h3>
    <p style="font-size: 0.95rem;">Теперь ваши устройства, подключенные к этому VPN, объединены в общую виртуальную сеть (10.8.0.x) и могут взаимодействовать друг с другом напрямую.</p>
    <div style="background: var(--bg); padding: 1rem; border-radius: 8px; border: 1px solid var(--border);">
      <p style="margin-top:0; font-size: 0.9rem; font-weight:600;">Проверка связи:</p>
      <p style="font-size: 0.85rem; margin-bottom: 0.5rem;">Попробуйте пропинговать IP-адрес нужного устройства. Если пинг не проходит на устройство с Windows, выполните на нём в PowerShell от имени Администратора:</p>
      <code style="display:block; padding: 0.75rem; background: #000; color: #4ade80; border-radius: 6px; font-size: 0.8rem; overflow-x: auto;">
New-NetFirewallRule -DisplayName "Allow Ping from VPN" -Direction Inbound -Protocol ICMPv4 -IcmpType 8 -RemoteAddress 10.8.0.0/24 -Action Allow
      </code>
    </div>
    <button class="btn btn-primary" style="margin-top: 1rem; width: 100%;" onclick="closeModal('lan-modal')">Понятно, закрыть</button>
  </div>
</div>

<div id="update-modal" class="modal">
  <div class="modal-content">
    <button class="modal-close" onclick="closeModal('update-modal')">&times;</button>
    <h3>Доступны обновления</h3>
    <div id="update-list" class="update-list"></div>
    <div style="background: var(--bg); padding: 1rem; border-radius: 8px; margin-top: 1rem; border: 1px solid var(--border);">
      <p style="margin-top:0; font-size: 0.9rem; color: var(--text-muted);">Для обновления выполните в консоли сервера:</p>
      <code style="display:block; padding: 0.5rem; background: #000; color: #4ade80; border-radius: 6px; font-size: 0.85rem; overflow-x: auto;">
        sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/daff4ik/amneziawg-cascade/main/deploy.sh)" -- --update
      </code>
    </div>
  </div>
</div>

<div id="global-loader" class="global-loader">
  <div class="spinner"></div>
  <div id="loader-text" style="font-weight: 500;">Подождите...</div>
</div>

<script>
const BASE_URL = "__BASE__";
let _allClients = [];
let _serversData = { active_id: null, servers: [] };
let _currentSort = 'name';
let _sortAsc = true;
let _pollInterval = null;

function initTheme() {
  const saved = localStorage.getItem('theme') || 'light';
  document.documentElement.setAttribute('data-theme', saved);
  document.getElementById('theme-btn').textContent = saved === 'dark' ? '☀️' : '🌙';
}
function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme');
  const next = current === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
  document.getElementById('theme-btn').textContent = next === 'dark' ? '☀️' : '🌙';
}

function showLoader(txt) { document.getElementById('loader-text').textContent = txt; document.getElementById('global-loader').style.display = 'flex'; }
function hideLoader() { document.getElementById('global-loader').style.display = 'none'; }
function closeModal(id) { document.getElementById(id).style.display = 'none'; }
function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function formatDateTime(val) {
  if (!val) return '';
  const d = val instanceof Date ? val : new Date(val);
  if (isNaN(d)) return val;
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getDate())}.${pad(d.getMonth()+1)}.${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function switchTab(tabId) {
  document.querySelectorAll('.view-section').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById(`${tabId}-view`).classList.add('active');
  document.querySelector(`button[onclick="switchTab('${tabId}')"]`).classList.add('active');
  if (tabId === 'servers') loadServers();
}

// ==================== LAN Settings ====================
async function initLanStatus() {
  try {
    const r = await fetch(BASE_URL + '/api/lan/status', {credentials:'same-origin'});
    if (r.ok) {
        const d = await r.json();
        document.getElementById('lan-toggle').checked = d.enabled;
    }
  } catch(e) {}
}

async function toggleLanStatus() {
  const chk = document.getElementById('lan-toggle');
  const enable = chk.checked;
  
  showLoader('Применение правил маршрутизации...');
  try {
    const r = await fetch(BASE_URL + '/api/lan/toggle', {
        method: 'POST', credentials:'same-origin', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled: enable})
    });
    if (!r.ok) throw new Error('Ошибка сервера');
    
    if (enable) {
        document.getElementById('lan-modal').style.display = 'flex';
    }
  } catch (e) {
    chk.checked = !enable; // Откат UI
    alert('Ошибка при изменении сетевых настроек: ' + e.message);
  } finally {
    hideLoader();
  }
}

// ==================== Clients ====================
function setSort(col) {
  if (_currentSort === col) { _sortAsc = !_sortAsc; } 
  else { _currentSort = col; _sortAsc = true; }
  
  document.querySelectorAll('th[data-sort]').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.getAttribute('data-sort') === col) th.classList.add(_sortAsc ? 'sort-asc' : 'sort-desc');
  });
  applyFilters();
}

function applyFilters() {
  const query = document.getElementById('search-input').value.toLowerCase();
  const status = document.getElementById('status-filter').value;
  
  let filtered = _allClients.filter(c => {
    const matchQuery = c.name.toLowerCase().includes(query) || c.ip.includes(query);
    const matchStatus = status === 'all' || c.status === status;
    return matchQuery && matchStatus;
  });

  filtered.sort((a, b) => {
    let valA = a[_currentSort]; let valB = b[_currentSort];
    if (_currentSort === 'ip') {
        const arrA = (valA || '').split('.');
        const arrB = (valB || '').split('.');
        for (let i = 0; i < 4; i++) {
          const numA = parseInt(arrA[i], 10) || 0;
          const numB = parseInt(arrB[i], 10) || 0;
          if (numA !== numB) return _sortAsc ? numA - numB : numB - numA;
        }
        return 0;
    }
    if (typeof valA === 'string') valA = valA.toLowerCase();
    if (typeof valB === 'string') valB = valB.toLowerCase();
    if (valA < valB) return _sortAsc ? -1 : 1;
    if (valA > valB) return _sortAsc ? 1 : -1;
    return 0;
  });

  renderClients(filtered);
}

function renderClients(clients) {
  const tbody = document.getElementById('clients-tbody');
  const empty = document.getElementById('empty-state');
  document.getElementById('clients-count').textContent = _allClients.length;
  
  if (clients.length === 0) {
    tbody.innerHTML = ''; empty.style.display = 'block'; return;
  }
  empty.style.display = 'none';

  tbody.innerHTML = clients.map(c => {
    const statusClass = c.status === 'ONLINE' ? 'status-online' : (c.status === 'OFFLINE' ? 'status-offline' : 'status-never');
    const statusLabel = c.status === 'ONLINE' ? 'ОНЛАЙН' : (c.status === 'OFFLINE' ? 'ОФФЛАЙН' : 'НИКОГДА');
    const encName = encodeURIComponent(c.name);
    const safeName = escapeHtml(c.name);
    
    return `<tr>
      <td data-label="Имя"><strong>${safeName}</strong></td>
      <td data-label="IP">${escapeHtml(c.ip)}</td>
      <td data-label="Статус"><span class="status-badge ${statusClass}">${statusLabel}</span></td>
      <td data-label="RX">${escapeHtml(c.rx)}</td>
      <td data-label="TX">${escapeHtml(c.tx)}</td>
      <td data-label="Handshake">${escapeHtml(c.handshake)}</td>
      <td data-label="Действия" class="actions-cell">
        <a href="${BASE_URL}/download/conf/${encName}" class="btn btn-small" title="Скачать .conf">.conf</a>
        <a href="${BASE_URL}/download/vpn/${encName}" class="btn btn-small" title="Amnezia Link">vpn://</a>
        <form method="post" action="${BASE_URL}/delete/${encName}" onsubmit="return confirm('Удалить клиента ${safeName}?');" style="margin:0;">
          <button type="submit" class="btn btn-small btn-danger" title="Удалить">×</button>
        </form>
      </td>
    </tr>`;
  }).join('');
  document.getElementById('last-update').textContent = formatDateTime(new Date());
}

async function fetchClients() {
  try {
    const r = await fetch(BASE_URL + '/api/clients', {credentials:'same-origin', cache:'no-store'});
    if (r.status === 401) { location.reload(); return; }
    if (r.ok) { _allClients = await r.json(); applyFilters(); }
  } catch (e) { console.error(e); }
}

// ==================== Servers ====================
async function loadServers() {
  try {
    const r = await fetch(BASE_URL + '/api/servers', {credentials:'same-origin'});
    if (!r.ok) return;
    _serversData = await r.json();
    const tbody = document.getElementById('servers-tbody');
    if (!_serversData.servers.length) {
      tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text-muted);">Нет добавленных серверов</td></tr>';
      return;
    }
    tbody.innerHTML = _serversData.servers.map(s => {
      const isActive = s.id === _serversData.active_id;
      const statusHtml = isActive ? `<span class="status-badge status-online">Активный</span>` : `<span class="status-badge status-never">Ожидает</span>`;
      const safeName = escapeHtml(s.name);
      let actions = isActive 
        ? `<span style="color:var(--text-muted); font-size: 0.85rem;">Используется</span>`
        : `<button class="btn btn-small btn-primary" onclick="switchServer(${s.id}, '${safeName.replace(/'/g, "\\'")}')">Переключить</button>
           <button class="btn btn-small btn-danger" onclick="deleteServer(${s.id})">×</button>`;
      return `<tr><td data-label="Имя"><strong>${safeName}</strong></td><td data-label="IP">${escapeHtml(s.ip)}</td><td data-label="Статус">${statusHtml}</td><td data-label="Действия" style="text-align:right;">${actions}</td></tr>`;
    }).join('');
  } catch (e) { console.error(e); }
}

async function addServer() {
  const ip = document.getElementById('add-ip').value.trim();
  const port = parseInt(document.getElementById('add-port').value) || 22;
  const user = document.getElementById('add-user').value.trim() || 'root';
  const name = document.getElementById('add-name').value.trim() || ip;
  const pass = document.getElementById('add-pass').value;
  if (!ip || !pass) { alert('IP и пароль обязательны!'); return; }
  const btn = document.getElementById('add-server-btn');
  btn.disabled = true; btn.textContent = '⏳ Подключение...';
  try {
    const r = await fetch(BASE_URL + '/api/servers/add', {
      method: 'POST', credentials:'same-origin', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ip, port, user, password: pass, name})
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Server error');
    document.getElementById('add-ip').value = ''; document.getElementById('add-pass').value = ''; document.getElementById('add-name').value = '';
    await loadServers();
  } catch(e) { alert('Ошибка: ' + e.message); } 
  finally { btn.disabled = false; btn.textContent = 'Проверить и добавить'; }
}

async function switchServer(sid, name) {
  if (!confirm(`Переключиться на сервер ${name}?\nВам нужно будет ввести его SSH-пароль.`)) return;
  const pass = prompt(`SSH-пароль для ${name}:`);
  if (!pass) return;
  showLoader(`Настройка туннеля на ${name}... Не закрывайте страницу!`);
  try {
    const r = await fetch(BASE_URL + `/api/servers/switch/${sid}`, {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({password: pass})
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Switch failed');
    alert('✅ Успешно! ' + (d.message || ''));
    await loadServers();
  } catch (e) { alert('❌ Ошибка: ' + e.message); } 
  finally { hideLoader(); }
}

async function deleteServer(sid) {
  if (!confirm('Удалить сервер из списка панели?')) return;
  await fetch(BASE_URL + `/api/servers/${sid}`, {method: 'DELETE', credentials: 'same-origin'});
  await loadServers();
}

function restartTunnels() {
  if (!confirm('Перезапустить туннели awg0 и awg1? VPN соединение прервется на пару секунд.')) return;
  const f = document.createElement('form'); f.method = 'POST'; f.action = BASE_URL + '/restart';
  document.body.appendChild(f); f.submit();
}

async function checkUpdates() {
  showLoader('Проверка обновлений...');
  try {
    const r = await fetch(BASE_URL + '/api/check_update', {credentials: 'same-origin'});
    if (r.status === 401) { location.reload(); return; }
    const d = await r.json();
    hideLoader();
    if (d.error) { alert('Ошибка: ' + d.error); }
    else if (!d.has_updates) { alert('У вас установлена последняя версия.'); }
    else {
      document.getElementById('update-list').innerHTML = d.commits.map(c => 
        `<div class="update-item"><div style="color:var(--text-muted);font-size:0.8rem;">${formatDateTime(c.date)}</div>${escapeHtml(c.message)}</div>`
      ).join('');
      document.getElementById('update-modal').style.display = 'flex';
    }
  } catch(e) { hideLoader(); alert(e); }
}

function startPolling() {
  if (_pollInterval) clearInterval(_pollInterval);
  fetchClients();
  _pollInterval = setInterval(fetchClients, 5000);
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearInterval(_pollInterval); 
  else startPolling();
});

initTheme();
initLanStatus();
document.querySelector('th[data-sort="ip"]').classList.add('sort-asc');
setSort('ip');
startPolling();

</script>
</body>
</html>"""

def render_login(error=""):
    err = f'<div class="error-msg">{error}</div>' if error else ""
    return LOGIN_TEMPLATE.replace("__ERR__", err).replace("__LABEL__", SERVER_LABEL).replace("__BASE__", BASE_URL)

def render_page(clients, message="", error=""):
    msg = f'<div class="alert ok">✅ {message}</div>' if message else ""
    err = f'<div class="alert err">❌ {error}</div>' if error else ""
    trx, ttx = total_traffic(clients)
    return (PAGE_TEMPLATE
            .replace("__MSG__", msg)
            .replace("__ERR__", err)
            .replace("__LABEL__", SERVER_LABEL)
            .replace("__COUNT__", str(len(clients)))
            .replace("__TOTAL_RX__", trx)
            .replace("__TOTAL_TX__", ttx)
            .replace("__BASE__", BASE_URL))

# ================ Routes ================
app = FastAPI(docs_url=None, redoc_url=None)

@app.exception_handler(RedirectToLogin)
async def _rtl(request: Request, exc: RedirectToLogin):
    return RedirectResponse(f"{BASE_URL}/login", status_code=303)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    try:
        _, p = load_credentials()
        if hmac.compare_digest(request.cookies.get(SESSION_COOKIE, ""), make_token(p)):
            return RedirectResponse(f"{BASE_URL}/", status_code=303)
    except RuntimeError:
        pass
    return render_login(error)

@app.post("/login")
async def login_submit(username: str = Form(...), password: str = Form(...)):
    try:
        u, p = load_credentials()
    except RuntimeError:
        return RedirectResponse(f"{BASE_URL}/login?error=Config+Error", status_code=303)
    
    if not (hmac.compare_digest(username, u) and hmac.compare_digest(password, p)):
        return RedirectResponse(f"{BASE_URL}/login?error=Неверный+логин+или+пароль", status_code=303)
    
    r = RedirectResponse(f"{BASE_URL}/", status_code=303)
    r.set_cookie(SESSION_COOKIE, make_token(p), max_age=SESSION_DAYS*86400,
                 httponly=True, secure=True, samesite="lax", path="/")
    return r

@app.get("/logout")
async def logout():
    r = RedirectResponse(f"{BASE_URL}/login", status_code=303)
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
    return render_page([], message, error)

@app.get("/api/clients")
async def api_clients(request: Request):
    current_user(request)
    return JSONResponse(enrich(parse_clients()))

# --- API Локальной Сети (LAN) ---
@app.get("/api/lan/status")
async def api_lan_status(request: Request):
    current_user(request)
    enabled = False
    try:
        with open(AWG_CONF, "r", encoding="utf-8") as f:
            enabled = "iptables -I FORWARD -i awg1 -o awg1 -j ACCEPT" in f.read()
    except Exception:
        pass
    return JSONResponse({"enabled": enabled})

@app.post("/api/lan/toggle")
async def api_lan_toggle(request: Request):
    current_user(request)
    body = await request.json()
    enable = bool(body.get("enabled", False))
    
    lan_rules = [
        "PostUp = iptables -I FORWARD -i awg1 -o awg1 -j ACCEPT\n",
        "PostUp = ip route add 10.8.0.0/24 dev awg1 table 100 || true\n",
        "PostDown = iptables -D FORWARD -i awg1 -o awg1 -j ACCEPT\n",
        "PostDown = ip route del 10.8.0.0/24 dev awg1 table 100 || true\n"
    ]
    
    try:
        with open(AWG_CONF, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        # Очищаем конфиг от этих правил на случай, если они уже есть
        clean_lines = [l for l in lines if l not in lan_rules]
        
        if enable:
            # Вставляем правила перед секцией [Peer] или в конец блока Interface
            insert_idx = len(clean_lines)
            for i, line in enumerate(clean_lines):
                if line.strip().startswith("[Peer]"):
                    insert_idx = i
                    break
            clean_lines = clean_lines[:insert_idx] + lan_rules + clean_lines[insert_idx:]
            
        with open(AWG_CONF, "w", encoding="utf-8") as f:
            f.writelines(clean_lines)
            
        # Мягкий рестарт клиентского интерфейса
        subprocess.run(["systemctl", "restart", "awg-quick@awg1"], capture_output=True, check=True)
        
        state_str = "ВКЛЮЧЕНА" if enable else "ВЫКЛЮЧЕНА"
        _notify(f"🌐 <b>Локальная сеть (LAN)</b>\nСвязь между клиентами {state_str}")
        
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
# ---------------------------------

@app.get("/api/check_update")
async def api_check_update(request: Request):
    current_user(request)
    result = await run_in_threadpool(check_github_updates)
    return JSONResponse(result)

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
    if "error" in result: return JSONResponse(result, status_code=400)
    return result

@app.delete("/api/servers/{sid}")
async def api_servers_del(request: Request, sid: int):
    current_user(request)
    data = load_servers()
    if data.get("active_id") == sid: return JSONResponse({"error": "Нельзя удалить активный сервер"}, status_code=400)
    data["servers"] = [s for s in data["servers"] if s["id"] != sid]
    save_servers(data)
    return {"ok": True}

@app.post("/api/servers/switch/{sid}")
async def api_servers_switch(request: Request, sid: int):
    current_user(request)
    try: body = await request.json(); password = body.get("password", "")
    except Exception: password = ""
    if not password: return JSONResponse({"error": "Требуется SSH-пароль"}, status_code=400)
    data = load_servers()
    server = next((s for s in data["servers"] if s["id"] == sid), None)
    if not server: return JSONResponse({"error": "Сервер не найден"}, status_code=404)
    if data.get("active_id") == sid: return JSONResponse({"error": "Этот сервер уже активен"}, status_code=400)
    
    ok, msg = await run_in_threadpool(do_switch_server, server, password)
    if ok:
        _notify(f"🌐 <b>Переключение SERVER-B</b>\nНа: <code>{server['ip']}</code> ({server['name']})\nРезультат: OK")
        return {"ok": True, "message": msg}
    else:
        _notify(f"❌ <b>Ошибка переключения SERVER-B</b>\nНа: <code>{server['ip']}</code>\n{msg[:300]}")
        return JSONResponse({"error": msg}, status_code=500)

@app.post("/create")
async def create_client(request: Request, name: str = Form(...)):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    if not n: return RedirectResponse(f"{BASE_URL}/?error=Недопустимое+имя+клиента", status_code=303)
    try:
        subprocess.run([ADD_SCRIPT, n], input="y\n", capture_output=True, text=True, timeout=90, check=True)
        _notify(f"👤 <b>Новый клиент</b> (через панель)\nИмя: <code>{n}</code>")
        return RedirectResponse(f"{BASE_URL}/?message=Клиент+{n}+создан", status_code=303)
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "error").strip()[:200]
        return RedirectResponse(f"{BASE_URL}/?error={err.replace(' ','+').replace(chr(10),' ')}", status_code=303)
    except subprocess.TimeoutExpired:
        return RedirectResponse(f"{BASE_URL}/?error=Таймаут+операции", status_code=303)

@app.post("/delete/{name}")
async def delete_client(request: Request, name: str):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    try:
        subprocess.run([REMOVE_SCRIPT, n], input="y\n", capture_output=True, text=True, timeout=60, check=True)
        _notify(f"🗑️ <b>Клиент удалён</b> (через панель)\nИмя: <code>{n}</code>")
        return RedirectResponse(f"{BASE_URL}/?message=Клиент+{n}+удалён", status_code=303)
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "error").strip()[:200]
        return RedirectResponse(f"{BASE_URL}/?error={err.replace(' ','+').replace(chr(10),' ')}", status_code=303)

@app.post("/restart")
async def restart_tunnels(request: Request):
    current_user(request)
    res = []
    for iface in ("awg0", "awg1"):
        try:
            subprocess.run(["systemctl", "restart", f"awg-quick@{iface}"], capture_output=True, timeout=30, check=True)
            res.append(f"{iface}: OK")
        except Exception: res.append(f"{iface}: FAIL")
    msg = " | ".join(res)
    _notify(f"🔄 <b>Перезапуск</b>\n{msg}")
    return RedirectResponse(f"{BASE_URL}/?message={msg.replace(' ','+')}", status_code=303)

@app.get("/download/conf/{name}")
async def dl_conf(request: Request, name: str):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    p = os.path.join(CLIENTS_DIR, f"{n}.conf")
    if not os.path.isfile(p): raise HTTPException(404, "Конфигурация не найдена")
    return FileResponse(p, media_type="text/plain", filename=f"{n}.conf")

@app.get("/download/vpn/{name}")
async def dl_vpn(request: Request, name: str):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    p = os.path.join(CLIENTS_DIR, f"{n}.vpn.txt")
    if not os.path.isfile(p): raise HTTPException(404, "Ссылка не найдена")
    return FileResponse(p, media_type="text/plain", filename=f"{n}.vpn.txt")

if __name__ == "__main__":
    kwargs = { "host": BIND_HOST, "port": 8000, "log_level": "info", "access_log": False, "server_header": False }
    if USE_SSL:
        kwargs["ssl_keyfile"] = "/etc/amnezia-panel-ssl/key.pem"
        kwargs["ssl_certfile"] = "/etc/amnezia-panel-ssl/cert.pem"
    uvicorn.run(app, **kwargs)