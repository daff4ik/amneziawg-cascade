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
    for block in re.split(r"\n(?=[Peer])", content):
        if "[Peer]" not in block:
            continue
        nm = re.search(r