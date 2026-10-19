#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AmneziaWG Web Panel — с графиками трафика."""
import os
import re
import io
import hmac
import hashlib
import secrets
import sqlite3
import subprocess
import time
from fastapi import FastAPI, HTTPException, Depends, Form, Request
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response, JSONResponse
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
    return cfg["username"], cfg["password"]

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

# ================ Парсинг ================
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

def awg_stats():
    try:
        r = subprocess.run(["awg", "show", AWG_IFACE], capture_output=True,
                           text=True, timeout=10, check=True)
    except Exception:
        return {}
    stats, cur = {}, None
    for line in r.stdout.splitlines():
        s = line.strip()
        m = re.match(r"^peer:\s*(\S+)", s)
        if m:
            cur = m.group(1)
            stats[cur] = {"handshake": "never", "rx": "0 B", "tx": "0 B"}
            continue
        if not cur:
            continue
        m = re.match(r"^latest handshake:\s*(.+)", s)
        if m:
            stats[cur]["handshake"] = m.group(1); continue
        m = re.match(r"^transfer:\s*(.+?)\s+received,\s*(.+?)\s+sent", s)
        if m:
            stats[cur]["rx"] = m.group(1); stats[cur]["tx"] = m.group(2)
    return stats

def status_of(hs):
    if hs == "never":
        return "NEVER"
    m = re.match(r"^(\d+)\s+second", hs)
    if m:
        return "ONLINE"
    m = re.match(r"^(\d+)\s+minute", hs)
    if m:
        return "ONLINE" if int(m.group(1)) < 3 else "OFFLINE"
    return "OFFLINE"

def parse_bytes(s):
    if not s:
        return 0.0
    m = re.match(r"^\s*([\d.]+)\s*([KMG]?i?B|B)\s*$", str(s), re.I)
    if not m:
        return 0.0
    v = float(m.group(1))
    u = m.group(2).upper().replace("I", "")
    return v * {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}.get(u, 1)

def fmt_bytes(b):
    if b >= 1024**3: return f"{b/1024**3:.2f} GiB"
    if b >= 1024**2: return f"{b/1024**2:.2f} MiB"
    if b >= 1024:    return f"{b/1024:.2f} KiB"
    return f"{int(b)} B"

def enrich(clients):
    st = awg_stats()
    for c in clients:
        s = st.get(c["pubkey"], {})
        c["handshake"] = s.get("handshake", "never")
        c["rx"]        = s.get("rx", "0 B")
        c["tx"]        = s.get("tx", "0 B")
        c["status"]    = status_of(c["handshake"])
    return clients

def total_traffic(clients):
    rx = sum(parse_bytes(c.get("rx", "0 B")) for c in clients)
    tx = sum(parse_bytes(c.get("tx", "0 B")) for c in clients)
    return fmt_bytes(rx), fmt_bytes(tx)

# ================ Статистика из SQLite ================
def traffic_series(name: str, period: str):
    """Возвращает {'labels': [...], 'rx': [...], 'tx': [...]} для Chart.js.
    rx/tx — скорость в байт/сек между точками."""
    if not os.path.exists(TRAFFIC_DB):
        return {"labels": [], "rx": [], "tx": [], "error": "no data yet"}

    now = int(time.time())
    conf = {
        "1h":  (now - 3600,    300),     # 12 точек
        "24h": (now - 86400,   1800),    # 48 точек
        "7d":  (now - 7*86400, 7200),    # 84 точки
    }
    start, bucket = conf.get(period, conf["24h"])

    conn = sqlite3.connect(TRAFFIC_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if name == "total":
        cur.execute("""
            SELECT pubkey, ts, rx_total, tx_total
            FROM traffic WHERE ts >= ?
            ORDER BY pubkey, ts
        """, (start,))
    else:
        cur.execute("""
            SELECT pubkey, ts, rx_total, tx_total
            FROM traffic WHERE name = ? AND ts >= ?
            ORDER BY pubkey, ts
        """, (name, start))

    rows = cur.fetchall()
    conn.close()

    # Разбиваем по pubkey, считаем дельты, агрегируем в bucket
    from collections import defaultdict
    buckets = defaultdict(lambda: {"rx": 0.0, "tx": 0.0})
    prev = {}
    for row in rows:
        pk = row["pubkey"]
        ts = row["ts"]
        rx = row["rx_total"]
        tx = row["tx_total"]
        if pk in prev:
            p_ts, p_rx, p_tx = prev[pk]
            d_rx = rx - p_rx
            d_tx = tx - p_tx
            # если awg1 перезапускался — delta < 0, пропускаем
            if d_rx >= 0 and d_tx >= 0:
                b = (ts // bucket) * bucket
                buckets[b]["rx"] += d_rx
                buckets[b]["tx"] += d_tx
        prev[pk] = (ts, rx, tx)

    labels = []
    rxs = []
    txs = []
    for b in sorted(buckets.keys()):
        labels.append(time.strftime("%H:%M", time.localtime(b)) if period in ("1h","24h")
                      else time.strftime("%d.%m %H:%M", time.localtime(b)))
        rxs.append(round(buckets[b]["rx"], 1))
        txs.append(round(buckets[b]["tx"], 1))
    return {"labels": labels, "rx": rxs, "tx": txs}

# ================ HTML: login ================
LOGIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="light"><head>
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
<div class="wrap"><div class="card"><h2>Sign in</h2>__ERR__
<form method="post" action="/login">
  <input type="text" name="username" placeholder="Username" required autofocus autocomplete="username">
  <input type="password" name="password" placeholder="Password" required autocomplete="current-password">
  <button type="submit">Sign in</button>
</form></div></div>
<script>(function(){var t=localStorage.getItem('theme')||'light';document.documentElement.setAttribute('data-theme',t);})();</script>
</body></html>"""

# ================ HTML: main ================
PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="light"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AmneziaWG Panel</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root{--bg:#f2f2f7;--card:#fff;--text:#1d1d1f;--muted:#6e6e73;--border:#ececf0;--input-bg:#fff;--input-border:#d2d2d7;--accent:#0071e3;--accent-hover:#0077ed;--btn-bg:#e8e8ed;--btn-bg-hover:#dcdce1;--btn-danger-bg:#ffe5e5;--btn-danger-text:#c00;--btn-danger-hover:#ffcccc;--ok-bg:#e5f7ec;--ok-text:#006c2b;--ok-border:#009e3d;--err-bg:#ffe5e5;--err-text:#a00;--err-border:#c00;--header-bg:#1d1d1f;--header-text:#fff;--online:#009e3d;--offline:#ff9500;--never:#8e8e93}
  html[data-theme=dark]{--bg:#1c1c1e;--card:#2c2c2e;--text:#f5f5f7;--muted:#98989d;--border:#3a3a3c;--input-bg:#1c1c1e;--input-border:#48484a;--accent:#0a84ff;--accent-hover:#409cff;--btn-bg:#3a3a3c;--btn-bg-hover:#48484a;--btn-danger-bg:#4a1e1e;--btn-danger-text:#ff6961;--btn-danger-hover:#5c2424;--ok-bg:#1a3a24;--ok-text:#6bd68a;--ok-border:#30d158;--err-bg:#3a1a1a;--err-text:#ff6961;--err-border:#ff453a;--header-bg:#000;--header-text:#fff;--online:#30d158;--offline:#ffd60a;--never:#8e8e93}
  *{box-sizing:border-box;transition:background-color .15s,color .15s,border-color .15s}
  body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:var(--bg);color:var(--text)}
  header{background:var(--header-bg);color:var(--header-text);padding:1em 2em;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:1em}
  header h1{margin:0;font-size:1.15em;font-weight:600}
  header .sub{font-size:.85em;opacity:.7}
  .header-actions{display:flex;gap:.5em;align-items:center;flex-wrap:wrap}
  .traffic-summary{display:flex;flex-direction:column;gap:.15em;padding:.4em .9em;margin-right:.5em;border:1px solid rgba(255,255,255,.25);border-radius:8px;font-size:.78em;background:rgba(255,255,255,.05);font-family:SF Mono,Consolas,Menlo,monospace;letter-spacing:.02em;cursor:pointer;transition:background .15s}
  .traffic-summary:hover{background:rgba(255,255,255,.15)}
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
  .client-name{color:var(--accent);cursor:pointer;font-weight:600}
  .client-name:hover{text-decoration:underline}
  .alert{padding:.9em 1.2em;border-radius:8px;margin-bottom:1em;font-size:.92em}
  .alert.ok{background:var(--ok-bg);color:var(--ok-text);border-left:4px solid var(--ok-border)}
  .alert.err{background:var(--err-bg);color:var(--err-text);border-left:4px solid var(--err-border)}
  .refresh-indicator{font-size:.78em;color:var(--muted);text-align:right;margin-bottom:.4em}
  .modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.6);z-index:1000}
  .modal-content{background:var(--card);color:var(--text);padding:2em;border-radius:12px;max-width:900px;width:90%;margin:3em auto;position:relative;max-height:90vh;overflow-y:auto}
  .modal-content img{max-width:100%;height:auto;background:#fff;padding:1em;border-radius:8px}
  .close{position:absolute;top:.5em;right:1em;font-size:1.8em;cursor:pointer;color:var(--muted);line-height:1}
  .close:hover{color:var(--text)}
  .period-btns{display:flex;gap:.5em;margin:1em 0}
  .period-btns button{background:var(--btn-bg);color:var(--text);padding:.5em 1em;font-size:.85em}
  .period-btns button.active{background:var(--accent);color:#fff}
  #traffic-chart{max-height:400px}
</style></head><body>
<header>
  <div><h1>🛡️ AmneziaWG Panel</h1><div class="sub">__LABEL__</div></div>
  <div class="header-actions">
    <div class="traffic-summary" id="total-traffic-btn" title="Click for total traffic chart">
      <div class="traffic-row"><span class="traffic-label">⬇ RX:</span><span class="traffic-value" id="total-rx">__TOTAL_RX__</span></div>
      <div class="traffic-row"><span class="traffic-label">⬆ TX:</span><span class="traffic-value" id="total-tx">__TOTAL_TX__</span></div>
    </div>
    <button id="restart-btn" class="btn ghost">🔄 Restart</button>
    <button id="theme-toggle" class="btn ghost">🌙</button>
    <a href="/logout" class="btn ghost">🚪 Logout</a>
  </div>
</header>
<div class="container">
  __MSG__ __ERR__
  <div class="card">
    <h3>➕ Create new client</h3>
    <form method="post" action="/create">
      <input type="text" name="name" placeholder="Client name (a-z, 0-9, _, -)" pattern="[a-zA-Z0-9_-]+" required autocomplete="off">
      <button type="submit">Create</button>
    </form>
  </div>
  <div class="card">
    <h3>👥 Clients (<span id="clients-count">__COUNT__</span>)</h3>
    <div class="refresh-indicator">Updated: <span id="last-update">--</span></div>
    <table><thead><tr><th>Name</th><th>IP</th><th>Status</th><th>RX (from client)</th><th>TX (to client)</th><th>Handshake</th><th>Actions</th></tr></thead>
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

<!-- Traffic chart modal -->
<div id="traffic-modal" class="modal"><div class="modal-content">
  <span class="close" onclick="closeTraffic()">&times;</span>
  <h3 id="traffic-title">Traffic</h3>
  <div class="period-btns">
    <button data-period="1h" onclick="loadTraffic(currentTrafficTarget,'1h',this)">1h</button>
    <button data-period="24h" onclick="loadTraffic(currentTrafficTarget,'24h',this)" class="active">24h</button>
    <button data-period="7d" onclick="loadTraffic(currentTrafficTarget,'7d',this)">7d</button>
  </div>
  <canvas id="traffic-chart"></canvas>
</div></div>

<script>
var currentTrafficTarget = null;
var trafficChart = null;

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
    if (trafficChart) {
      var dark = next === 'dark';
      trafficChart.options.scales.x.ticks.color = dark ? '#98989d' : '#6e6e73';
      trafficChart.options.scales.y.ticks.color = dark ? '#98989d' : '#6e6e73';
      trafficChart.options.scales.x.grid.color = dark ? '#3a3a3c' : '#ececf0';
      trafficChart.options.scales.y.grid.color = dark ? '#3a3a3c' : '#ececf0';
      trafficChart.update();
    }
  });

  document.getElementById('restart-btn').addEventListener('click', function(){
    if (!confirm('Restart awg0 and awg1?')) return;
    var f = document.createElement('form'); f.method='POST'; f.action='/restart';
    document.body.appendChild(f); f.submit();
  });

  document.getElementById('total-traffic-btn').addEventListener('click', function(){
    openTraffic('total', 'Total traffic');
  });
})();

function closeTraffic(){
  document.getElementById('traffic-modal').style.display='none';
  if (trafficChart) { trafficChart.destroy(); trafficChart = null; }
}

function openTraffic(target, title){
  currentTrafficTarget = target;
  document.getElementById('traffic-title').textContent = title;
  document.getElementById('traffic-modal').style.display = 'block';
  document.querySelectorAll('.period-btns button').forEach(function(b){ b.classList.remove('active'); });
  document.querySelector('.period-btns button[data-period="24h"]').classList.add('active');
  loadTraffic(target, '24h', null);
}

async function loadTraffic(target, period, btn){
  if (btn) {
    document.querySelectorAll('.period-btns button').forEach(function(b){ b.classList.remove('active'); });
    btn.classList.add('active');
  }
  var url = target === 'total' ? '/api/stats/total?period=' + period
                               : '/api/stats/' + encodeURIComponent(target) + '?period=' + period;
  try {
    var r = await fetch(url, { credentials: 'same-origin' });
    if (r.status === 401) { location.href = '/login'; return; }
    var data = await r.json();
    renderChart(data);
  } catch(e) {
    alert('Failed to load chart: ' + e);
  }
}

function fmtBytes(b){
  if (b >= 1073741824) return (b/1073741824).toFixed(2) + ' GiB';
  if (b >= 1048576)    return (b/1048576).toFixed(2) + ' MiB';
  if (b >= 1024)       return (b/1024).toFixed(2) + ' KiB';
  return Math.round(b) + ' B';
}

function renderChart(data){
  var ctx = document.getElementById('traffic-chart').getContext('2d');
  if (trafficChart) trafficChart.destroy();
  var dark = document.documentElement.getAttribute('data-theme') === 'dark';
  trafficChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: data.labels,
      datasets: [
        {
          label: 'RX (from clients)',
          data: data.rx,
          borderColor: '#4ade80',
          backgroundColor: 'rgba(74,222,128,0.1)',
          fill: true, tension: 0.3, pointRadius: 2, borderWidth: 2
        },
        {
          label: 'TX (to clients)',
          data: data.tx,
          borderColor: '#60a5fa',
          backgroundColor: 'rgba(96,165,250,0.1)',
          fill: true, tension: 0.3, pointRadius: 2, borderWidth: 2
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: dark ? '#f5f5f7' : '#1d1d1f' } },
        tooltip: {
          callbacks: {
            label: function(ctx){ return ctx.dataset.label + ': ' + fmtBytes(ctx.parsed.y); }
          }
        }
      },
      scales: {
        x: { ticks: { color: dark ? '#98989d' : '#6e6e73' }, grid: { color: dark ? '#3a3a3c' : '#ececf0' } },
        y: {
          ticks: { color: dark ? '#98989d' : '#6e6e73', callback: function(v){ return fmtBytes(v); } },
          grid: { color: dark ? '#3a3a3c' : '#ececf0' },
          beginAtZero: true
        }
      }
    }
  });
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
  var cn = e.target.closest ? e.target.closest('.client-name') : null;
  if (cn) {
    e.preventDefault();
    var n = cn.getAttribute('data-name');
    openTraffic(n, 'Traffic: ' + n);
    return;
  }
  if (e.target.id === 'qr-modal') document.getElementById('qr-modal').style.display = 'none';
  if (e.target.id === 'traffic-modal') closeTraffic();
});

(function(){
  function statusClass(s){ return 's-' + String(s).toLowerCase(); }
  function esc(s){ var d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
  function parseBytes(s){
    if (!s) return 0;
    var m = String(s).match(/^\s*([\d.]+)\s*([KMG]?i?B|B)\s*$/i);
    if (!m) return 0;
    var mult = { 'B':1,'KB':1024,'MB':1048576,'GB':1073741824 };
    return parseFloat(m[1]) * (mult[m[2].toUpperCase().replace('I','')] || 1);
  }
  function fmtBytes(b){
    if (b >= 1073741824) return (b/1073741824).toFixed(2)+' GiB';
    if (b >= 1048576)    return (b/1048576).toFixed(2)+' MiB';
    if (b >= 1024)       return (b/1024).toFixed(2)+' KiB';
    return Math.round(b)+' B';
  }
  function buildRow(c){
    return '<tr>'+
      '<td><span class="client-name" data-name="'+esc(c.name)+'">'+esc(c.name)+'</span></td>'+
      '<td>'+esc(c.ip)+'</td>'+
      '<td class="'+statusClass(c.status)+'">'+esc(c.status)+'</td>'+
      '<td>'+esc(c.rx)+'</td>'+
      '<td>'+esc(c.tx)+'</td>'+
      '<td>'+esc(c.handshake)+'</td>'+
      '<td>'+
        '<a href="/download/conf/'+encodeURIComponent(c.name)+'" class="btn">.conf</a>'+
        '<a href="/download/vpn/'+encodeURIComponent(c.name)+'" class="btn">vpn://</a>'+
        '<button type="button" class="btn qr-btn" data-name="'+esc(c.name)+'">QR</button>'+
        '<form method="post" action="/delete/'+encodeURIComponent(c.name)+'" onsubmit="return confirm(\'Delete client '+esc(c.name)+'?\')" style="display:inline">'+
        '<button type="submit" class="btn danger">Delete</button></form>'+
      '</td></tr>';
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
      var tb = document.getElementById('clients-tbody');
      tb.innerHTML = clients.length === 0
        ? '<tr><td colspan="7" class="empty">No clients yet</td></tr>'
        : clients.map(buildRow).join('');
      document.getElementById('clients-count').textContent = clients.length;
      document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
      var rxs = 0, txs = 0;
      clients.forEach(function(c){ rxs += parseBytes(c.rx); txs += parseBytes(c.tx); });
      document.getElementById('total-rx').textContent = fmtBytes(rxs);
      document.getElementById('total-tx').textContent = fmtBytes(txs);
    } catch(e) {} finally { updating = false; }
  }
  setInterval(refresh, 5000);
  setTimeout(refresh, 500);
})();
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
            f'<td><span class="client-name" data-name="{n}">{n}</span></td>'
            f'<td>{c["ip"]}</td>'
            f'<td class="s-{cls}">{st}</td>'
            f'<td>{c.get("rx","0 B")}</td>'
            f'<td>{c.get("tx","0 B")}</td>'
            f'<td>{c.get("handshake","never")}</td>'
            '<td>'
            f'<a href="/download/conf/{n}" class="btn">.conf</a>'
            f'<a href="/download/vpn/{n}" class="btn">vpn://</a>'
            f'<button type="button" class="btn qr-btn" data-name="{n}">QR</button>'
            f'<form method="post" action="/delete/{n}" onsubmit="return confirm(\'Delete client {n}?\')" style="display:inline">'
            '<button type="submit" class="btn danger">Delete</button></form>'
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

@app.get("/api/stats/{name}")
async def api_stats(request: Request, name: str, period: str = "24h"):
    current_user(request)
    if period not in ("1h", "24h", "7d"):
        period = "24h"
    name_clean = re.sub(r"[^a-zA-Z0-9_-]", "", name)
    return JSONResponse(traffic_series(name_clean, period))

@app.get("/api/stats/total")
async def api_stats_total(request: Request, period: str = "24h"):
    current_user(request)
    if period not in ("1h", "24h", "7d"):
        period = "24h"
    return JSONResponse(traffic_series("total", period))

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