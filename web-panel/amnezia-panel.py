#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AmneziaWG Web Panel — упрощенная версия с накопительным трафиком."""
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
from fastapi import FastAPI, HTTPException, Depends, Form, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response, JSONResponse
from starlette.concurrency import run_in_threadpool
import qrcode
import qrcode.image.svg
import uvicorn

CONFIG_FILE   = "/etc/amnezia-panel.conf"
AWG_IFACE     = "awg1"
AWG_CONF      = f"/etc/amnezia/amneziawg/{AWG_IFACE}.conf"
CLIENTS_DIR   = "/root/clients"
ADD_SCRIPT    = "/usr/local/bin/amnezia-add-client.sh"
REMOVE_SCRIPT = "/usr/local/bin/amnezia-remove-client.sh"
NOTIFY_SCRIPT = "/usr/local/bin/amnezia-notify.sh"
TRAFFIC_DB    = "/var/lib/amnezia-panel/traffic.db"
SERVER_LABEL  = "__SERVER_LABEL__"
UPDATE_SCRIPT = "/usr/local/bin/amnezia-update.sh"
COMMIT_STATE_FILE = "/var/lib/amnezia-panel/installed_commit"
REPO_API_URL      = "https://api.github.com/repos/davideprikyan/amneziawg-cascade/commits"

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
    requires_ssh = False
    
    for c in commits:
        sha = c["sha"]
        if sha == installed_sha:
            break
        msg = c["commit"]["message"]
        date = c["commit"]["author"]["date"]
        new_commits.append({"sha": sha, "message": msg, "date": date})
        
        try:
            files_url = f"https://api.github.com/repos/davideprikyan/amneziawg-cascade/commits/{sha}"
            req_f = urllib.request.Request(files_url, headers={'User-Agent': 'Amnezia-Panel'})
            with urllib.request.urlopen(req_f, timeout=5) as resp_f:
                data = json.loads(resp_f.read().decode())
                for f in data.get("files", []):
                    if f["filename"] == "deploy.sh" or f["filename"].endswith(".service"):
                        requires_ssh = True
        except Exception:
            pass

    return {
        "has_updates": len(new_commits) > 0,
        "commits": new_commits[:10],
        "requires_ssh": requires_ssh,
        "latest_sha": commits[0]["sha"] if commits else ""
    }

# ================ Парсинг и Статистика ================
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
    """Возвращает статус (онлайн/оффлайн) на основе свежих данных awg"""
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
                    mins = int(re.search(r"(\d+)\s+minutes", s).group(1))
                    if mins < 3:
                        stats[cur] = "ONLINE"
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
                db_stats[row[0]] = {
                    "total_rx": row[1],
                    "total_tx": row[2],
                    "last_handshake": row[3]
                }
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
    rx = 0
    tx = 0
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

# ================ HTML: login ================
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
  button:hover{opacity:.9}
  .error{background:var(--err-bg);color:var(--err-text);padding:.8em;border-radius:8px;margin-bottom:1em;text-align:center;font-size:.9em}
</style></head><body>
<header><h1>🛡️ AmneziaWG Panel</h1><div class="sub">__LABEL__</div></header>
<div class="wrap"><div class="card"><h2 data-i18n="signin">Вход</h2>__ERR__
<form method="post" action="/login">
  <input type="text" name="username" placeholder="Username" required autofocus autocomplete="username">
  <input type="password" name="password" placeholder="Password" required autocomplete="current-password">
  <button type="submit" data-i18n="btn_login">Войти</button>
</form></div></div>
<script>
(function(){
  var t=localStorage.getItem('theme')||'light';
  document.documentElement.setAttribute('data-theme',t);
  var lang=localStorage.getItem('lang')||'ru';
  document.querySelectorAll('[data-i18n]').forEach(function(el){
    var k=el.getAttribute('data-i18n');
    if(k==='signin') el.textContent = lang==='ru'?'Вход':'Sign in';
    if(k==='btn_login') el.textContent = lang==='ru'?'Войти':'Sign in';
  });
})();
</script>
</body></html>"""

# ================ HTML: main ================
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
  .traffic-summary{display:flex;flex-direction:column;gap:.15em;padding:.4em .9em;margin-right:.5em;border:1px solid rgba(255,255,255,.25);border-radius:8px;font-size:.78em;background:rgba(255,255,255,.05);font-family:SF Mono,Consolas,Menlo,monospace;letter-spacing:.02em;align-self:center;}
  .traffic-row{display:flex;gap:.5em;align-items:center;white-space:nowrap}
  .traffic-label{opacity:.65;min-width:3.2em}
  .traffic-value{color:#4ade80;font-weight:600}
  .traffic-row:nth-child(2) .traffic-value{color:#60a5fa}
  @media(max-width:640px){.traffic-summary{font-size:.7em;padding:.3em .6em}.traffic-label{min-width:2.8em}}
  .btn.ghost{background:transparent;color:var(--header-text);border:1px solid rgba(255,255,255,.25);padding:.5em .9em;font-size:.85em;margin:0}
  .btn.ghost:hover{background:rgba(255,255,255,.1)}
  .container{max-width:1200px;margin:2em auto;padding:0 1em}
  .card{background:var(--card);border-radius:12px;padding:1.5em;margin-bottom:1.2em;box-shadow:0 1px 3px rgba(0,0,0,.06)}
  .card h3{margin:0 0 1em;font-size:1em;font-weight:600}
  input[type=text]{width:100%;padding:.75em;border:1px solid var(--input-border);border-radius:8px;font-size:1em;margin-bottom:.6em;background:var(--input-bg);color:var(--text)}
  button,.btn{padding:.6em 1.2em;border:none;border-radius:8px;background:var(--accent);color:#fff;cursor:pointer;font-size:.9em;text-decoration:none;display:inline-block}
  button:hover{background:var(--accent-hover)}
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
  .modal-content{background:var(--card);color:var(--text);padding:2em;border-radius:12px;max-width:700px;width:90%;margin:3em auto;position:relative;max-height:90vh;overflow-y:auto}
  .modal-content img{max-width:100%;height:auto;background:#fff;padding:1em;border-radius:8px}
  .close{position:absolute;top:.5em;right:1em;font-size:1.8em;cursor:pointer;color:var(--muted);line-height:1}
  .close:hover{color:var(--text)}
  .update-list{margin:1em 0;max-height:300px;overflow-y:auto;text-align:left;font-size:0.9em;border:1px solid var(--border);border-radius:8px;padding:0.5em}
  .update-item{padding:0.5em;border-bottom:1px solid var(--border)}
  .update-item:last-child{border-bottom:none}
  .update-date{color:var(--muted);font-size:0.85em}
  .ssh-warn{color:var(--err-text);background:var(--err-bg);padding:1em;border-radius:8px;margin-bottom:1em;font-weight:bold}
  .modal-actions{display:flex;justify-content:flex-end;gap:1em;margin-top:1.5em}
  .lang-dropdown { position: relative; display: inline-block; }
  .lang-menu { display: none; position: absolute; right: 0; background-color: var(--card); min-width: 130px; box-shadow: 0 8px 16px 0 rgba(0,0,0,0.2); z-index: 1001; border-radius: 8px; border: 1px solid var(--border); overflow: hidden; margin-top: 5px; }
  .lang-menu a { color: var(--text); padding: 10px 16px; text-decoration: none; display: block; cursor: pointer; font-size: 0.9em; }
  .lang-menu a:hover { background-color: var(--btn-bg); }
  .lang-menu.show { display: block; }
</style></head><body>
<header>
  <div><h1>🛡️ AmneziaWG Panel</h1><div class="sub">__LABEL__</div></div>
  <div class="header-actions">
    <div class="traffic-summary" title="Total traffic">
      <div class="traffic-row"><span class="traffic-label">⬇ RX:</span><span class="traffic-value" id="total-rx">__TOTAL_RX__</span></div>
      <div class="traffic-row"><span class="traffic-label">⬆ TX:</span><span class="traffic-value" id="total-tx">__TOTAL_TX__</span></div>
    </div>
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

<!-- QR modal -->
<div id="qr-modal" class="modal"><div class="modal-content">
  <span class="close" onclick="document.getElementById('qr-modal').style.display='none'">&times;</span>
  <h3>QR: <span id="qr-name"></span></h3>
  <p style="font-size:.85em;color:var(--muted)">Scan in AmneziaVPN</p>
  <img id="qr-img" src="" alt="QR">
</div></div>

<!-- Update Modal -->
<div id="update-modal" class="modal"><div class="modal-content">
  <span class="close" onclick="document.getElementById('update-modal').style.display='none'">&times;</span>
  <h3 data-i18n="update_avail">Доступны обновления</h3>
  <div id="update-ssh-warning" class="ssh-warn" style="display:none;" data-i18n="update_ssh_warn">
    ⚠️ Внимание! Одно из обновлений затрагивает системные файлы. Для его полного применения потребуется вход по SSH и запуск deploy.sh --update.
  </div>
  <div id="update-list" class="update-list"></div>
  <div class="modal-actions">
    <button class="btn" onclick="document.getElementById('update-modal').style.display='none'" data-i18n="btn_ignore">Игнорировать</button>
    <button class="btn" style="background:var(--accent);color:#fff" id="do-update-btn" onclick="doUpdate()" data-i18n="btn_update">Обновить</button>
  </div>
</div></div>

<script>
var latestShaToSave = "";
var currentLang = localStorage.getItem('lang') || 'ru';

const i18n = {
  ru: {
    signin: "Вход", btn_login: "Войти", btn_restart: "🔄 Перезапуск", btn_logout: "🚪 Выход",
    create_title: "➕ Создать нового клиента", btn_create: "Создать",
    clients_title: "👥 Клиенты", updated: "Обновлено:",
    th_name: "Имя", th_ip: "IP", th_status: "Статус", th_rx: "RX (всего)", th_tx: "TX (всего)", th_handshake: "Последний handshake", th_actions: "Действия",
    update_avail: "Доступны обновления", update_ssh_warn: "⚠️ Внимание! Одно из обновлений затрагивает системные файлы. Для его полного применения потребуется вход по SSH и запуск deploy.sh --update.",
    btn_ignore: "Игнорировать", btn_update: "Обновить", no_updates: "Обновлений не найдено. У вас последняя версия.", err_check: "Ошибка проверки обновлений: ",
    updating: "Обновление...", btn_check_update: "🔄 Проверить обновления", checking: "⏳ Проверка..."
  },
  en: {
    signin: "Sign in", btn_login: "Sign in", btn_restart: "🔄 Restart", btn_logout: "🚪 Logout",
    create_title: "➕ Create new client", btn_create: "Create",
    clients_title: "👥 Clients", updated: "Updated:",
    th_name: "Name", th_ip: "IP", th_status: "Status", th_rx: "RX (total)", th_tx: "TX (total)", th_handshake: "Last handshake", th_actions: "Actions",
    update_avail: "Updates Available", update_ssh_warn: "⚠️ Warning! Some updates affect system files. You will need SSH access and deploy.sh --update to apply them fully.",
    btn_ignore: "Ignore", btn_update: "Update", no_updates: "No updates found. You are on the latest version.", err_check: "Update check error: ",
    updating: "Updating...", btn_check_update: "🔄 Check Update", checking: "⏳ Checking..."
  }
};

function t(key) { return i18n[currentLang][key] || key; }

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
  if (menu && menu.classList.contains('show')) {
    menu.classList.remove('show');
  }
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

  // ===== Check Update =====
  var _ub = document.getElementById('check-update-btn');
  if (_ub) {
    _ub.addEventListener('click', async function(){
      this.disabled = true;
      this.textContent = t('checking');
      try {
        var r = await fetch('/api/check_update', {credentials: 'same-origin'});
        if (r.status === 401) { location.href = '/login'; return; }
        var d = await r.json();
        
        if (d.error) {
          alert(t('err_check') + d.error);
        } else if (!d.has_updates) {
          alert(t('no_updates'));
        } else {
          latestShaToSave = d.latest_sha;
          var listDiv = document.getElementById('update-list');
          listDiv.innerHTML = d.commits.map(c => 
            '<div class="update-item"><div class="update-date">' + new Date(c.date).toLocaleString() + '</div>' + 
            c.message.replace(/\\n/g, '<br>') + '</div>'
          ).join('');
          
          document.getElementById('update-ssh-warning').style.display = d.requires_ssh ? 'block' : 'none';
          document.getElementById('update-modal').style.display = 'block';
        }
      } catch(e) {
        alert(t('err_check') + e);
      } finally {
        this.disabled = false;
        this.textContent = t('btn_check_update');
      }
    });
  }

  document.getElementById('restart-btn').addEventListener('click', function(){
    if (!confirm('Restart awg0 and awg1?')) return;
    var f = document.createElement('form'); f.method='POST'; f.action='/restart';
    document.body.appendChild(f); f.submit();
  });

  applyLang();
})();

async function doUpdate() {
  var btn = document.getElementById('do-update-btn');
  btn.disabled = true;
  btn.textContent = '⏳ ' + t('updating');
  try {
    var r = await fetch('/api/update', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sha: latestShaToSave})
    });
    if (r.ok) {
      setTimeout(function(){ location.reload(); }, 20000);
    } else {
      alert('Update failed: HTTP ' + r.status);
      btn.disabled = false;
      btn.textContent = t('btn_update');
    }
  } catch(e) {
    alert('Error: ' + e);
    btn.disabled = false;
    btn.textContent = t('btn_update');
  }
}

document.addEventListener('click', function(e){
  var qr = e.target.closest ? e.target.closest('.qr-btn') : null;
  if (qr) {
    e.preventDefault();
    var nm = qr.getAttribute('data-name');
    document.getElementById('qr-name').textContent = nm;
    document.getElementById('qr-img').src = '/qr/' + encodeURIComponent(nm) + '?_=' + Date.now();
    document.getElementById('qr-modal').style.display = 'block';
    return;
  }
  if (e.target.id === 'qr-modal') document.getElementById('qr-modal').style.display = 'none';
});

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
      '<button type="button" class="btn qr-btn" data-name="'+esc(c.name)+'">QR</button>'+
      '<form method="post" action="/delete/'+encodeURIComponent(c.name)+'" onsubmit="return confirm(\''+(currentLang==='ru'?'Удалить клиента ':'Delete client ')+esc(c.name)+'?\')" style="display:inline">'+
      '<button type="submit" class="btn danger">'+btnDelete+'</button></form>'+
    '</td></tr>';
}

function esc(s){ var d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

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
    
    // Суммарный трафик из шапки обновляется через API /api/clients (возвращает актуальные данные)
    // Нам нужно запросить сервер для получения суммы, но мы уже получаем клиентов. 
    // Оставим как есть, сервер сам пришлет актуальные цифры в HTML.
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
            f'<button type="button" class="btn qr-btn" data-name="{n}">QR</button>'
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

# ================ Роуты ================
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

@app.post("/api/update")
async def api_update(request: Request):
    current_user(request)
    if not os.path.exists(UPDATE_SCRIPT):
        raise HTTPException(500, "update script not found.")
    
    try:
        body = await request.json()
        sha_to_save = body.get("sha", "")
    except:
        sha_to_save = ""

    try:
        if sha_to_save:
            with open(COMMIT_STATE_FILE, "w") as f:
                f.write(sha_to_save)

        subprocess.run([
            "systemd-run",
            "--collect",
            "--unit=amnezia-update-" + str(int(time.time())),
            "--on-active=2s",
            UPDATE_SCRIPT,
        ], check=False, timeout=10)
        return {"status": "started"}
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

@app.post("/create")
async def create_client(request: Request, name: str = Form(...)):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    if not n:
        return RedirectResponse("/?error=Invalid+name", status_code=303)
    try:
        subprocess.run([ADD_SCRIPT, n], input="y\n", capture_output=True,
                       text=True, timeout=90, check=True)
        _notify(f"👤 <b>Новый клиент</b> (через панель)\nИмя: <code>{n}</code>\nХост: <code>{os.uname().nodename}</code>")
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
        subprocess.run([REMOVE_SCRIPT, n], input="y\n", capture_output=True,
                       text=True, timeout=60, check=True)
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
    _notify(f"🔄 <b>Перезапуск туннелей</b> (через панель)\n{msg}")
    return RedirectResponse(f"/?message={msg.replace(' ','+')}", status_code=303)

@app.get("/download/conf/{name}")
async def dl_conf(request: Request, name: str):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    p = os.path.join(CLIENTS_DIR, f"{n}.conf")
    if not os.path.isfile(p):
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type="text/plain", filename=f"{n}.conf")

@app.get("/download/vpn/{name}")
async def dl_vpn(request: Request, name: str):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    p = os.path.join(CLIENTS_DIR, f"{n}.vpn.txt")
    if not os.path.isfile(p):
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type="text/plain", filename=f"{n}.vpn.txt")

@app.get("/qr/{name}")
async def qr(request: Request, name: str):
    current_user(request)
    n = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    p = os.path.join(CLIENTS_DIR, f"{n}.vpn.txt")
    if not os.path.isfile(p):
        raise HTTPException(404, "not found")
    with open(p, encoding="utf-8") as f:
        content = f.read().strip()
    img = qrcode.make(content, image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO(); img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info",
                access_log=False, server_header=False,
                ssl_keyfile="/etc/amnezia-panel-ssl/key.pem",
                ssl_certfile="/etc/amnezia-panel-ssl/cert.pem")