#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AmneziaWG Web Panel — версия с формой логина вместо Basic Auth.
"""
import os
import re
import io
import hmac
import hashlib
import secrets
import subprocess
from fastapi import FastAPI, HTTPException, Depends, Form, Request
from fastapi.responses import (
    HTMLResponse, FileResponse, RedirectResponse, Response, JSONResponse,
)
import qrcode
import qrcode.image.svg
import uvicorn

# ==================== Конфигурация ====================
CONFIG_FILE   = "/etc/amnezia-panel.conf"
AWG_IFACE     = "awg1"
AWG_CONF      = f"/etc/amnezia/amneziawg/{AWG_IFACE}.conf"
CLIENTS_DIR   = "/root/clients"
ADD_SCRIPT    = "/usr/local/bin/amnezia-add-client.sh"
REMOVE_SCRIPT = "/usr/local/bin/amnezia-remove-client.sh"
SERVER_LABEL  = "__SERVER_LABEL__"

SESSION_COOKIE = "amnezia_session"
SESSION_DAYS   = 7

# ==================== Аутентификация ====================
def load_credentials():
    if not os.path.exists(CONFIG_FILE):
        raise RuntimeError(f"Config not found: {CONFIG_FILE}")
    cfg = {}
    with open(CONFIG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    if "username" not in cfg or "password" not in cfg:
        raise RuntimeError("username and password required")
    return cfg["username"], cfg["password"]

def make_session_token(password: str) -> str:
    """Детерминированный токен из пароля. Меняется при смене пароля."""
    return hmac.new(password.encode("utf-8"), b"amnezia-panel-session", hashlib.sha256).hexdigest()

def verify_session_token(token: str, password: str) -> bool:
    if not token or not password:
        return False
    return hmac.compare_digest(token, make_session_token(password))

class RedirectToLogin(Exception):
    """Специальный сигнал для HTML-страниц: перенаправить на /login."""
    pass

def current_user(request: Request) -> str:
    """Зависимость для всех защищённых роутов."""
    try:
        u, p = load_credentials()
    except RuntimeError:
        raise HTTPException(status_code=500, detail="Panel config error")

    token = request.cookies.get(SESSION_COOKIE, "")
    if not verify_session_token(token, p):
        # для API возвращаем 401, для страниц — редирект
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=401, detail="Unauthorized")
        raise RedirectToLogin()
    return u

# ==================== Парсинг состояния ====================
def parse_clients():
    if not os.path.exists(AWG_CONF):
        return []
    with open(AWG_CONF, encoding="utf-8") as f:
        content = f.read()
    blocks = re.split(r"\n(?=\[Peer\])", content)
    clients = []
    for block in blocks:
        if "[Peer]" not in block:
            continue
        name_m = re.search(r"#\s*Client:\s*(\S+)", block)
        pub_m  = re.search(r"PublicKey\s*=\s*(\S+)", block)
        ip_m   = re.search(r"AllowedIPs\s*=\s*([0-9.]+)", block)
        if name_m and pub_m:
            clients.append({
                "name":   name_m.group(1),
                "pubkey": pub_m.group(1),
                "ip":     ip_m.group(1) if ip_m else "?",
            })
    return clients

def get_awg_stats():
    try:
        r = subprocess.run(["awg", "show", AWG_IFACE],
                           capture_output=True, text=True, timeout=10, check=True)
    except Exception:
        return {}
    stats = {}
    cur = None
    for line in r.stdout.splitlines():
        line = line.strip()
        m = re.match(r"^peer:\s*(\S+)", line)
        if m:
            cur = m.group(1)
            stats[cur] = {"handshake": "never", "rx": "0 B", "tx": "0 B"}
            continue
        if cur is None:
            continue
        m = re.match(r"^latest handshake:\s*(.+)", line)
        if m:
            stats[cur]["handshake"] = m.group(1)
            continue
        m = re.match(r"^transfer:\s*(.+?)\s+received,\s*(.+?)\s+sent", line)
        if m:
            stats[cur]["rx"] = m.group(1)
            stats[cur]["tx"] = m.group(2)
    return stats

def classify_status(handshake):
    if handshake == "never":
        return "NEVER"
    m = re.match(r"^(\d+)\s+second", handshake)
    if m:
        return "ONLINE"
    m = re.match(r"^(\d+)\s+minute", handshake)
    if m:
        return "ONLINE" if int(m.group(1)) < 3 else "OFFLINE"
    return "OFFLINE"

def parse_bytes(s):
    if not s:
        return 0.0
    m = re.match(r"^\s*([\d.]+)\s*([KMG]?i?B|B)\s*$", str(s), re.IGNORECASE)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2).upper().replace("I", "")
    mult = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}
    return val * mult.get(unit, 1)

def format_bytes(b):
    if b >= 1024 ** 3:
        return f"{b / 1024**3:.2f} GiB"
    if b >= 1024 ** 2:
        return f"{b / 1024**2:.2f} MiB"
    if b >= 1024:
        return f"{b / 1024:.2f} KiB"
    return f"{int(b)} B"

def total_traffic(clients):
    rx_sum = sum(parse_bytes(c.get("rx", "0 B")) for c in clients)
    tx_sum = sum(parse_bytes(c.get("tx", "0 B")) for c in clients)
    return format_bytes(rx_sum), format_bytes(tx_sum)

def enrich(clients):
    stats = get_awg_stats()
    for c in clients:
        s = stats.get(c["pubkey"], {})
        c["handshake"] = s.get("handshake", "never")
        c["rx"]        = s.get("rx", "0 B")
        c["tx"]        = s.get("tx", "0 B")
        c["status"]    = classify_status(c["handshake"])
    return clients

# ==================== HTML: LOGIN ====================
LOGIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login — AmneziaWG Panel</title>
<style>
  :root {
    --bg:#f2f2f7; --card:#fff; --text:#1d1d1f; --muted:#6e6e73;
    --border:#d2d2d7; --accent:#0071e3; --accent-hover:#0077ed;
    --err-bg:#ffe5e5; --err-text:#a00;
    --header-bg:#1d1d1f; --header-text:#fff;
  }
  html[data-theme="dark"] {
    --bg:#1c1c1e; --card:#2c2c2e; --text:#f5f5f7; --muted:#98989d;
    --border:#48484a; --accent:#0a84ff; --accent-hover:#409cff;
    --err-bg:#3a1a1a; --err-text:#ff6961;
    --header-bg:#000; --header-text:#fff;
  }
  * { box-sizing:border-box; }
  body { font-family:-apple-system,"Segoe UI",Roboto,sans-serif; margin:0; background:var(--bg); color:var(--text); min-height:100vh; display:flex; flex-direction:column; }
  header { background:var(--header-bg); color:var(--header-text); padding:1em 2em; }
  header h1 { margin:0; font-size:1.15em; font-weight:600; }
  header .sub { font-size:.85em; opacity:.7; }
  .wrap { flex:1; display:flex; align-items:center; justify-content:center; padding:2em 1em; }
  .card { background:var(--card); border-radius:12px; padding:2em; max-width:400px; width:100%; box-shadow:0 4px 20px rgba(0,0,0,.08); }
  .card h2 { margin:0 0 1.2em; font-size:1.2em; font-weight:600; text-align:center; }
  input[type=text], input[type=password] { width:100%; padding:.85em 1em; border:1px solid var(--border); border-radius:8px; font-size:1em; margin-bottom:.9em; background:var(--card); color:var(--text); }
  input:focus { outline:none; border-color:var(--accent); }
  button { width:100%; padding:.9em 1em; border:none; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; font-size:1em; font-weight:500; }
  button:hover { background:var(--accent-hover); }
  .error { background:var(--err-bg); color:var(--err-text); padding:.8em 1em; border-radius:8px; margin-bottom:1em; font-size:.9em; text-align:center; }
</style>
</head><body>
<header>
  <h1>&#128737; AmneziaWG Panel</h1>
  <div class="sub">__LABEL__</div>
</header>
<div class="wrap">
  <div class="card">
    <h2>Sign in</h2>
    __ERR__
    <form method="post" action="/login">
      <input type="text" name="username" placeholder="Username" required autofocus autocomplete="username">
      <input type="password" name="password" placeholder="Password" required autocomplete="current-password">
      <button type="submit">Sign in</button>
    </form>
  </div>
</div>
<script>
(function(){
  var t = localStorage.getItem('theme') || 'light';
  document.documentElement.setAttribute('data-theme', t);
})();
</script>
</body></html>
"""

# ==================== HTML: MAIN ====================
PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AmneziaWG Panel</title>
<style>
  :root {
    --bg:#f2f2f7; --card:#fff; --text:#1d1d1f; --muted:#6e6e73;
    --border:#ececf0; --input-bg:#fff; --input-border:#d2d2d7;
    --accent:#0071e3; --accent-hover:#0077ed;
    --btn-bg:#e8e8ed; --btn-bg-hover:#dcdce1;
    --btn-danger-bg:#ffe5e5; --btn-danger-text:#c00; --btn-danger-hover:#ffcccc;
    --ok-bg:#e5f7ec; --ok-text:#006c2b; --ok-border:#009e3d;
    --err-bg:#ffe5e5; --err-text:#a00; --err-border:#c00;
    --header-bg:#1d1d1f; --header-text:#fff;
    --online:#009e3d; --offline:#ff9500; --never:#8e8e93;
  }
  html[data-theme="dark"] {
    --bg:#1c1c1e; --card:#2c2c2e; --text:#f5f5f7; --muted:#98989d;
    --border:#3a3a3c; --input-bg:#1c1c1e; --input-border:#48484a;
    --accent:#0a84ff; --accent-hover:#409cff;
    --btn-bg:#3a3a3c; --btn-bg-hover:#48484a;
    --btn-danger-bg:#4a1e1e; --btn-danger-text:#ff6961; --btn-danger-hover:#5c2424;
    --ok-bg:#1a3a24; --ok-text:#6bd68a; --ok-border:#30d158;
    --err-bg:#3a1a1a; --err-text:#ff6961; --err-border:#ff453a;
    --header-bg:#000; --header-text:#fff;
    --online:#30d158; --offline:#ffd60a; --never:#8e8e93;
  }
  * { box-sizing:border-box; transition:background-color .15s, color .15s, border-color .15s; }
  body { font-family:-apple-system,"Segoe UI",Roboto,sans-serif; margin:0; background:var(--bg); color:var(--text); }
  header { background:var(--header-bg); color:var(--header-text); padding:1em 2em; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:1em; }
  header h1 { margin:0; font-size:1.15em; font-weight:600; }
  header .sub { font-size:.85em; opacity:.7; }
  .header-actions { display:flex; gap:.5em; align-items:center; flex-wrap:wrap; }
  .traffic-summary {
    display:flex; flex-direction:column; gap:.15em;
    padding:.4em .9em; margin-right:.5em;
    border:1px solid rgba(255,255,255,.25);
    border-radius:8px;
    font-size:.78em;
    background:rgba(255,255,255,.05);
    font-family:"SF Mono","Consolas","Menlo",monospace;
    letter-spacing:.02em;
    align-self:center;
  }
  .traffic-row { display:flex; gap:.5em; align-items:center; white-space:nowrap; }
  .traffic-label { opacity:.65; min-width:3.2em; }
  .traffic-value { color:#4ade80; font-weight:600; }
  .traffic-row:nth-child(2) .traffic-value { color:#60a5fa; }
  @media (max-width:640px) {
    .traffic-summary { font-size:.7em; padding:.3em .6em; }
    .traffic-label { min-width:2.8em; }
  }
  .btn.ghost { background:transparent; color:var(--header-text); border:1px solid rgba(255,255,255,.25); padding:.5em .9em; font-size:.85em; margin:0; }
  .btn.ghost:hover { background:rgba(255,255,255,.1); }
  .container { max-width:1200px; margin:2em auto; padding:0 1em; }
  .card { background:var(--card); border-radius:12px; padding:1.5em; margin-bottom:1.2em; box-shadow:0 1px 3px rgba(0,0,0,.06); }
  .card h3 { margin:0 0 1em; font-size:1em; font-weight:600; }
  input[type=text] { width:100%; padding:.75em; border:1px solid var(--input-border); border-radius:8px; font-size:1em; margin-bottom:.6em; background:var(--input-bg); color:var(--text); }
  button, .btn { padding:.6em 1.2em; border:none; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; font-size:.9em; text-decoration:none; display:inline-block; }
  button:hover { background:var(--accent-hover); }
  .btn { background:var(--btn-bg); color:var(--text); padding:.4em .8em; font-size:.82em; margin-right:.25em; }
  .btn:hover { background:var(--btn-bg-hover); }
  .btn.danger { background:var(--btn-danger-bg); color:var(--btn-danger-text); }
  .btn.danger:hover { background:var(--btn-danger-hover); }
  table { width:100%; border-collapse:collapse; }
  th, td { padding:.75em .5em; text-align:left; border-bottom:1px solid var(--border); font-size:.9em; }
  th { font-size:.72em; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); font-weight:600; }
  td.s-online { color:var(--online); font-weight:600; }
  td.s-offline { color:var(--offline); }
  td.s-never { color:var(--never); }
  td.empty { text-align:center; color:var(--muted); padding:2em; }
  .alert { padding:.9em 1.2em; border-radius:8px; margin-bottom:1em; font-size:.92em; }
  .alert.ok { background:var(--ok-bg); color:var(--ok-text); border-left:4px solid var(--ok-border); }
  .alert.err { background:var(--err-bg); color:var(--err-text); border-left:4px solid var(--err-border); }
  .refresh-indicator { font-size:.78em; color:var(--muted); text-align:right; margin-bottom:.4em; }
  .modal { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,.6); z-index:1000; }
  .modal-content { background:var(--card); color:var(--text); padding:2em; border-radius:12px; max-width:500px; margin:5em auto; text-align:center; position:relative; }
  .modal-content img { max-width:100%; height:auto; background:#fff; padding:1em; border-radius:8px; }
  .close { position:absolute; top:.5em; right:1em; font-size:1.8em; cursor:pointer; color:var(--muted); }
  .close:hover { color:var(--text); }
</style>
</head><body>
<header>
  <div>
    <h1>&#128737; AmneziaWG Panel</h1>
    <div class="sub">__LABEL__</div>
  </div>
  <div class="header-actions">
    <div class="traffic-summary" title="Total traffic across all clients">
      <div class="traffic-row">
        <span class="traffic-label">&#11015; RX:</span>
        <span class="traffic-value" id="total-rx">__TOTAL_RX__</span>
      </div>
      <div class="traffic-row">
        <span class="traffic-label">&#11014; TX:</span>
        <span class="traffic-value" id="total-tx">__TOTAL_TX__</span>
      </div>
    </div>
    <button id="restart-btn" class="btn ghost" title="Restart awg0 and awg1">&#128260; Restart</button>
    <button id="theme-toggle" class="btn ghost" title="Theme">&#127769;</button>
    <a href="/logout" class="btn ghost" title="Logout">&#128682; Logout</a>
  </div>
</header>
<div class="container">
  __MSG__
  __ERR__
  <div class="card">
    <h3>&#10133; Create new client</h3>
    <form method="post" action="/create">
      <input type="text" name="name" placeholder="Client name (a-z, 0-9, _, -)" pattern="[a-zA-Z0-9_-]+" required autocomplete="off">
      <button type="submit">Create</button>
    </form>
  </div>
  <div class="card">
    <h3>&#128101; Clients (<span id="clients-count">__COUNT__</span>)</h3>
    <div class="refresh-indicator">Updated: <span id="last-update">--</span></div>
    <table id="clients-table">
      <thead><tr>
        <th>Name</th><th>IP</th><th>Status</th>
        <th>RX (from client)</th><th>TX (to client)</th>
        <th>Handshake</th><th>Actions</th>
      </tr></thead>
      <tbody id="clients-tbody">__ROWS__</tbody>
    </table>
  </div>
</div>
<div id="qr-modal" class="modal">
  <div class="modal-content">
    <span class="close" onclick="document.getElementById('qr-modal').style.display='none'">&times;</span>
    <h3>QR: <span id="qr-name"></span></h3>
    <p style="font-size:.85em;color:var(--muted)">Scan in AmneziaVPN (vpn://)</p>
    <img id="qr-img" src="" alt="QR">
  </div>
</div>
<script>
(function(){
  var saved = localStorage.getItem('theme') || 'light';
  document.documentElement.setAttribute('data-theme', saved);
  var tt = document.getElementById('theme-toggle');
  tt.textContent = saved === 'dark' ? '\u2600\uFE0F' : '\uD83C\uDF19';
  tt.addEventListener('click', function(){
    var cur = document.documentElement.getAttribute('data-theme');
    var next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    tt.textContent = next === 'dark' ? '\u2600\uFE0F' : '\uD83C\uDF19';
  });

  document.getElementById('restart-btn').addEventListener('click', function(){
    if (!confirm('Restart awg0 and awg1?')) return;
    var f = document.createElement('form');
    f.method = 'POST'; f.action = '/restart';
    document.body.appendChild(f); f.submit();
  });

  document.addEventListener('click', function(e){
    var btn = e.target.closest ? e.target.closest('.qr-btn') : null;
    if (btn) {
      e.preventDefault();
      var name = btn.getAttribute('data-name');
      document.getElementById('qr-name').textContent = name;
      document.getElementById('qr-img').src = '/qr/' + encodeURIComponent(name) + '?_=' + Date.now();
      document.getElementById('qr-modal').style.display = 'block';
    }
    if (e.target === document.getElementById('qr-modal')) {
      document.getElementById('qr-modal').style.display = 'none';
    }
  });

  function statusClass(s){ return 's-' + String(s).toLowerCase(); }
  function esc(s){ var d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
  function parseBytes(s){
    if (!s) return 0;
    var m = String(s).match(/^\s*([\d.]+)\s*([KMG]?i?B|B)\s*$/i);
    if (!m) return 0;
    var v = parseFloat(m[1]);
    var u = m[2].toUpperCase().replace('I','');
    var mult = { 'B':1, 'KB':1024, 'MB':1048576, 'GB':1073741824 };
    return v * (mult[u] || 1);
  }
  function fmtBytes(b){
    if (b >= 1073741824) return (b/1073741824).toFixed(2)+' GiB';
    if (b >= 1048576)    return (b/1048576).toFixed(2)+' MiB';
    if (b >= 1024)       return (b/1024).toFixed(2)+' KiB';
    return Math.round(b)+' B';
  }
  function buildRow(c){
    return '<tr>' +
      '<td><strong>' + esc(c.name) + '</strong></td>' +
      '<td>' + esc(c.ip) + '</td>' +
      '<td class="' + statusClass(c.status) + '">' + esc(c.status) + '</td>' +
      '<td>' + esc(c.rx) + '</td>' +
      '<td>' + esc(c.tx) + '</td>' +
      '<td>' + esc(c.handshake) + '</td>' +
      '<td>' +
        '<a href="/download/conf/' + encodeURIComponent(c.name) + '" class="btn">.conf</a>' +
        '<a href="/download/vpn/'  + encodeURIComponent(c.name) + '" class="btn">vpn://</a>' +
        '<button type="button" class="btn qr-btn" data-name="' + esc(c.name) + '">QR</button>' +
        '<form method="post" action="/delete/' + encodeURIComponent(c.name) + '" onsubmit="return confirm(\'Delete client ' + esc(c.name) + '?\')" style="display:inline">' +
          '<button type="submit" class="btn danger">Delete</button>' +
        '</form>' +
      '</td>' +
    '</tr>';
  }
  var updating = false;
  async function refresh(){
    if (updating) return;
    updating = true;
    try {
      var r = await fetch('/api/clients', { credentials:'same-origin', cache:'no-store' });
      if (r.status === 401) { location.href = '/login'; return; }
      if (!r.ok) return;
      var clients = await r.json();
      var tbody = document.getElementById('clients-tbody');
      if (clients.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty">No clients yet</td></tr>';
      } else {
        tbody.innerHTML = clients.map(buildRow).join('');
      }
      document.getElementById('clients-count').textContent = clients.length;
      document.getElementById('last-update').textContent = new Date().toLocaleTimeString();

      var rxSum = 0, txSum = 0;
      for (var i=0; i<clients.length; i++){
        rxSum += parseBytes(clients[i].rx);
        txSum += parseBytes(clients[i].tx);
      }
      var rxEl = document.getElementById('total-rx');
      var txEl = document.getElementById('total-tx');
      if (rxEl) rxEl.textContent = fmtBytes(rxSum);
      if (txEl) txEl.textContent = fmtBytes(txSum);
    } catch(e) {} finally { updating = false; }
  }
  setInterval(refresh, 5000);
  setTimeout(refresh, 500);
})();
</script>
</body></html>
"""

def render_login(error=""):
    err_html = f'<div class="error">{error}</div>' if error else ""
    return LOGIN_TEMPLATE.replace("__ERR__", err_html).replace("__LABEL__", SERVER_LABEL)

def render_page(clients, message="", error=""):
    rows = []
    for c in clients:
        st = c.get("status", "NEVER")
        cls = st.lower()
        name = c["name"]
        rows.append(
            '<tr>'
            f'<td><strong>{name}</strong></td>'
            f'<td>{c["ip"]}</td>'
            f'<td class="s-{cls}">{st}</td>'
            f'<td>{c.get("rx","0 B")}</td>'
            f'<td>{c.get("tx","0 B")}</td>'
            f'<td>{c.get("handshake","never")}</td>'
            '<td>'
            f'<a href="/download/conf/{name}" class="btn">.conf</a>'
            f'<a href="/download/vpn/{name}" class="btn">vpn://</a>'
            f'<button type="button" class="btn qr-btn" data-name="{name}">QR</button>'
            f'<form method="post" action="/delete/{name}" onsubmit="return confirm(\'Delete client {name}?\')" style="display:inline">'
            '<button type="submit" class="btn danger">Delete</button>'
            '</form>'
            '</td>'
            '</tr>'
        )
    if not rows:
        rows.append('<tr><td colspan="7" class="empty">No clients yet</td></tr>')
    msg_html = f'<div class="alert ok">{message}</div>' if message else ""
    err_html = f'<div class="alert err">{error}</div>' if error else ""
    total_rx, total_tx = total_traffic(clients)
    return (PAGE_TEMPLATE
            .replace("__MSG__",   msg_html)
            .replace("__ERR__",   err_html)
            .replace("__LABEL__", SERVER_LABEL)
            .replace("__COUNT__", str(len(clients)))
            .replace("__ROWS__",  "".join(rows))
            .replace("__TOTAL_RX__", total_rx)
            .replace("__TOTAL_TX__", total_tx))

# ==================== Роуты ====================
app = FastAPI(docs_url=None, redoc_url=None)

# Обработчик сигнала RedirectToLogin — отправляем на /login
from fastapi.responses import RedirectResponse as _RR
from starlette.exceptions import HTTPException as StarletteHTTPException

@app.exception_handler(RedirectToLogin)
async def redirect_to_login_handler(request: Request, exc: RedirectToLogin):
    return _RR("/login", status_code=303)

# --- Публичные роуты ---
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    # если уже залогинен — сразу на главную
    try:
        _, p = load_credentials()
        token = request.cookies.get(SESSION_COOKIE, "")
        if verify_session_token(token, p):
            return _RR("/", status_code=303)
    except RuntimeError:
        pass
    return render_login(error)

@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    try:
        u, p = load_credentials()
    except RuntimeError:
        return _RR("/login?error=Panel+config+error", status_code=303)

    ok = (hmac.compare_digest(username, u)
          and hmac.compare_digest(password, p))
    if not ok:
        return _RR("/login?error=Invalid+credentials", status_code=303)

    token = make_session_token(p)
    resp = _RR("/", status_code=303)
    resp.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return resp

@app.get("/logout")
async def logout():
    resp = _RR("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp

# --- Защищённые роуты ---
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, message: str = "", error: str = ""):
    user = current_user(request)
    clients = enrich(parse_clients())
    return render_page(clients, message, error)

@app.get("/api/clients")
async def api_clients(request: Request):
    current_user(request)
    return JSONResponse(enrich(parse_clients()))

@app.post("/create")
async def create_client(request: Request, name: str = Form(...)):
    current_user(request)
    name_clean = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    if not name_clean:
        return _RR("/?error=Invalid+name", status_code=303)
    try:
        subprocess.run([ADD_SCRIPT, name_clean], input="y\n",
                       capture_output=True, text=True, timeout=90, check=True)
        return _RR(f"/?message=Client+{name_clean}+created", status_code=303)
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "unknown error").strip()[:200]
        return _RR(f"/?error={err.replace(' ','+').replace(chr(10),' ')}", status_code=303)
    except subprocess.TimeoutExpired:
        return _RR("/?error=Script+timeout+90s", status_code=303)

@app.post("/delete/{name}")
async def delete_client(request: Request, name: str):
    current_user(request)
    name_clean = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    if not name_clean:
        raise HTTPException(status_code=400, detail="Invalid name")
    try:
        subprocess.run([REMOVE_SCRIPT, name_clean], input="y\n",
                       capture_output=True, text=True, timeout=60, check=True)
        return _RR(f"/?message=Client+{name_clean}+deleted", status_code=303)
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or "error").strip()[:200]
        return _RR(f"/?error={err.replace(' ','+').replace(chr(10),' ')}", status_code=303)

@app.post("/restart")
async def restart_tunnels(request: Request):
    current_user(request)
    results = []
    for iface in ["awg0", "awg1"]:
        try:
            subprocess.run(["systemctl", "restart", f"awg-quick@{iface}"],
                           capture_output=True, text=True, timeout=30, check=True)
            results.append(f"{iface}: OK")
        except subprocess.CalledProcessError:
            results.append(f"{iface}: FAIL")
        except subprocess.TimeoutExpired:
            results.append(f"{iface}: TIMEOUT")
    msg = " | ".join(results)
    return _RR(f"/?message={msg.replace(' ','+')}", status_code=303)

@app.get("/download/conf/{name}")
async def dl_conf(request: Request, name: str):
    current_user(request)
    name_clean = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    path = os.path.join(CLIENTS_DIR, f"{name_clean}.conf")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, media_type="text/plain", filename=f"{name_clean}.conf")

@app.get("/download/vpn/{name}")
async def dl_vpn(request: Request, name: str):
    current_user(request)
    name_clean = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    path = os.path.join(CLIENTS_DIR, f"{name_clean}.vpn.txt")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="vpn:// link not found")
    return FileResponse(path, media_type="text/plain", filename=f"{name_clean}.vpn.txt")

@app.get("/qr/{name}")
async def qr(request: Request, name: str):
    current_user(request)
    name_clean = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    path = os.path.join(CLIENTS_DIR, f"{name_clean}.vpn.txt")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="vpn:// link not found")
    with open(path, encoding="utf-8") as f:
        content = f.read().strip()
    img = qrcode.make(content, image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    return Response(content=buf.getvalue(), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})

# ==================== Запуск ====================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info",
                access_log=False, server_header=False,
                ssl_keyfile="/etc/amnezia-panel-ssl/key.pem",
                ssl_certfile="/etc/amnezia-panel-ssl/cert.pem")