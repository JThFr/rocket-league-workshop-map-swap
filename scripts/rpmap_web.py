"""Local web UI and HTTP server for rpmap."""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

import rpmap_window
from rpmap_config import (APP_VERSION, DEFAULT_PORT, PRODUCT, SLOTS, SLOT_INFO,
                          TAILNET, clear_runtime, load_config, new_secret,
                          pid_alive, read_runtime, save_config, write_runtime)
from rpmap_maps import (get_preview_bytes, list_maps, map_inventory, map_key,
                        restore_slot, slot_state, use_map)
from rpmap_peers import gather_peers, local_identity, peer_switch, sync_action


# --------------------------------------------------------------------------- #
# Web UI
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>rpmap - custom map loader</title>
<style>
  :root{--bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--fg:#c9d1d9;--fg2:#8b949e;
        --accent:#238636;--accent2:#2ea043;--blue:#1f6feb;--warn:#d29922;--red:#da3633;--white:#f0f6fc}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
  header{padding:12px 22px;background:var(--bg2);border-bottom:1px solid #30363d;display:flex;gap:12px;align-items:center}
  h1{font-size:16px;margin:0;color:var(--white);font-weight:800;letter-spacing:.02em}
  .badge{padding:3px 10px;border-radius:999px;font-size:12px;font-weight:700;white-space:nowrap}
  .b-stock{background:#1f6feb33;color:#79c0ff;border:1px solid #1f6feb}
  .b-custom{background:#d2992233;color:#e3b341;border:1px solid var(--warn)}
  .b-unknown{background:#da363333;color:#ff7b72;border:1px solid var(--red)}
  .b-missing{background:#6e768133;color:#8b949e;border:1px solid #6e7681}
  main{padding:18px 22px;max-width:1240px;margin:0 auto}

  /* hero: what is currently loaded */
  .hero{display:flex;gap:18px;align-items:center;border-radius:14px;padding:16px 18px;margin-bottom:18px;
        background:linear-gradient(90deg,#1d2b3a,#161b22);border:1px solid #30465c}
  .hero.is-custom{background:linear-gradient(90deg,#3a2f14,#161b22);border-color:var(--warn)}
  .hero.is-unknown{background:linear-gradient(90deg,#3a1a1a,#161b22);border-color:var(--red)}
  .hero.is-missing{background:linear-gradient(90deg,#22262b,#161b22);border-color:#6e7681}
  .hero-thumb{width:210px;height:118px;flex:0 0 auto;border-radius:10px;overflow:hidden;position:relative;
              background:#0b0f14;border:1px solid #30363d}
  .hero-thumb img{width:100%;height:100%;object-fit:cover;display:block}
  .hero-thumb .ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--fg2);font-size:28px}
  .hero-info{min-width:0;flex:1}
  .hero-label{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--fg2)}
  .hero-name{font-size:30px;font-weight:800;color:var(--white);line-height:1.15;margin:2px 0 6px;
             overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .hero-sub{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .hero-actions{flex:0 0 auto;display:flex;flex-direction:column;gap:8px;align-items:stretch}
  .hint{margin-top:10px;color:var(--fg2);font-size:12.5px}

  .row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  .muted{color:var(--fg2);font-size:12.5px}
  .grow{flex:1}
  button{background:var(--bg3);color:var(--white);border:1px solid #30363d;border-radius:8px;
         padding:8px 14px;font-size:13.5px;font-weight:600;cursor:pointer}
  button:hover{background:#30363d}
  button.primary{background:var(--accent);border-color:var(--accent)}
  button.primary:hover{background:var(--accent2)}
  button.danger{background:transparent;border-color:var(--red);color:#ff7b72}
  button:disabled{opacity:.5;cursor:default}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
  .card{background:var(--bg2);border:1px solid #30363d;border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
  .card.is-active{border-color:var(--warn);box-shadow:0 0 0 1px var(--warn) inset}
  .thumb{position:relative;aspect-ratio:16/9;background:var(--bg3);overflow:hidden}
  .thumb img{width:100%;height:100%;object-fit:cover;display:block}
  .thumb .ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--fg2);font-size:22px}
  .card .body{padding:10px 12px;display:flex;flex-direction:column;gap:6px;flex:1}
  .card .name{color:var(--white);font-weight:700;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .card .meta{display:flex;justify-content:space-between;align-items:center;gap:8px}
  .src{font-size:11px;padding:2px 7px;border-radius:6px;background:var(--bg3);color:var(--fg2);text-transform:uppercase;letter-spacing:.04em}
  .search{background:var(--bg3);border:1px solid #30363d;border-radius:8px;color:var(--fg);padding:8px 12px;min-width:220px}
  .toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:var(--bg3);
          border:1px solid #30363d;border-radius:8px;padding:10px 16px;color:var(--white);opacity:0;
          transition:opacity .2s;pointer-events:none;z-index:9;max-width:80vw}
  .toast.show{opacity:1}
  .toast a{pointer-events:auto}
  code{background:var(--bg3);padding:1px 6px;border-radius:5px;color:#79c0ff}
  .panel{background:var(--bg2);border:1px solid #30363d;border-radius:10px;padding:14px 16px;margin-top:18px}
  table.peers{width:100%;border-collapse:collapse}
  table.peers th,table.peers td{text-align:left;padding:7px 8px;border-bottom:1px solid #21262d}
  table.peers th{color:var(--fg2);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em}
  table.peers tr:last-child td{border-bottom:0}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#6e7681;margin-right:6px}
  .dot.on{background:var(--accent2)}
  .dot.off{background:var(--red)}
  code.secret{font-size:14px;letter-spacing:.04em}
</style></head>
<body>
<header>
  <h1>rpmap</h1>
  <span class="muted" id="verLabel"></span>
  <span class="grow"></span>
  <span class="muted" id="pathLabel"></span>
  <button class="danger" id="quitBtn" onclick="quitApp()" title="Stop the rpmap server">Quit</button>
</header>
<main>
  <section class="hero" id="hero">
    <div class="hero-thumb" id="heroThumb"><span class="ph">&#127918;</span></div>
    <div class="hero-info">
      <div class="hero-label" id="heroLabel">Slot currently loads</div>
      <div class="hero-name" id="currentName">-</div>
      <div class="hero-sub">
        <span class="badge b-missing" id="slotBadge">loading...</span>
        <span class="muted" id="currentMeta"></span>
      </div>
      <div class="hint" id="heroHint">In game: pick the matching arena in Free Play.
        Restore the stock map before playing online.</div>
    </div>
    <div class="hero-actions">
      <button id="restoreBtn" class="danger" onclick="restore()">Restore stock map</button>
    </div>
  </section>

  <div class="row" style="margin-bottom:12px">
    <input class="search" id="search" placeholder="Filter maps..." oninput="render()">
    <label class="muted" id="everyoneLabel" title="Show only maps that every reachable peer running rpmap - and you - already have (all your maps when you're alone)">
      <input type="checkbox" id="everyoneOnly" onchange="render()"> Only maps everyone has
    </label>
    <span class="muted" id="count"></span>
    <span class="grow"></span>
    <button onclick="load_state()">Refresh</button>
  </div>

  <div class="grid" id="grid"></div>

  <div class="panel">
    <div class="row">
      <b>Players</b>
      <span class="grow"></span>
      <span class="muted" id="peersStatus">loading...</span>
      <button onclick="syncAll()" title="Push your loaded map to every reachable peer">Load mine on everyone</button>
      <button onclick="loadPeers()">Refresh</button>
    </div>
    <div class="row" style="margin-top:10px">
      <span class="muted">Session code</span>
      <code class="secret" id="mySecret">-</code>
      <button onclick="copySecret()">Copy</button>
      <span class="grow"></span>
      <input class="search" id="joinCode" placeholder="Paste a peer's session code...">
      <button onclick="joinSession()">Join</button>
    </div>
    <table class="peers" id="peerTable" style="margin-top:10px"></table>
    <div class="muted" id="peersEmpty" style="margin-top:6px;display:none">
      No peers discovered. Start tailscaled, run both machines on the tailnet with the
      same session code, and reload.
    </div>
  </div>
</main>
<div class="toast" id="toast"></div>
<script>
let STATE = null;
let TIMERS = [];        // poll intervals, cleared when the user quits
let COMMON = null;      // map keys that every reachable peer has, or null
let ARENA = 'the arena';
let ARENA_NOTE = '';
const token = new URLSearchParams(location.search).get('token') || '';
const api = (p, opts={}) => fetch(p + (p.includes('?')?'&':'?') + 'token=' + encodeURIComponent(token), opts);
const prevUrl = id => '/api/preview?id=' + encodeURIComponent(id) + '&token=' + encodeURIComponent(token);

function fmtSize(n){ if(!n) return ''; const u=['B','KB','MB','GB']; let i=0; while(n>=1024&&i<u.length-1){n/=1024;i++;} return n.toFixed(i?1:0)+' '+u[i]; }
function toast(msg, ms=2600){ const t=document.getElementById('toast'); t.textContent=msg; t.classList.add('show'); setTimeout(()=>t.classList.remove('show'), ms); }
function toastLink(msg, url, ms=6000){
  const t = document.getElementById('toast');
  t.textContent = '';
  t.appendChild(document.createTextNode(msg + (url ? '  ' : '')));
  if(url){
    const a = document.createElement('a');
    a.href = url; a.target = '_blank'; a.rel = 'noopener';
    a.textContent = 'Open Workshop \u2197';
    a.style.color = '#79c0ff'; a.style.textDecoration = 'underline';
    t.appendChild(a);
  }
  t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), ms);
}
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

async function load_state(){
  try { STATE = await (await api('/api/state')).json(); }
  catch(e){ toast('Cannot reach the local server'); return; }
  render();
}

function render(){
  if(!STATE) return;
  const s = STATE.slot;
  ARENA = s.arena || 'the arena';
  ARENA_NOTE = s.note || '';
  document.getElementById('pathLabel').textContent = STATE.cooked || '';
  document.getElementById('heroLabel').textContent = ARENA + ' slot currently loads';
  document.getElementById('heroHint').innerHTML =
    `In game: <b>Free Play &rarr; arena "${esc(ARENA)}"</b>` +
    (ARENA_NOTE ? ' &mdash; ' + esc(ARENA_NOTE) : '') +
    '. Restore the stock map before playing online.';

  const hero = document.getElementById('hero');
  hero.className = 'hero is-' + s.state;
  const name = s.map_name ? s.map_name
             : (s.state==='stock' ? 'Stock map'
             : (s.state==='missing' ? 'Slot file missing'
             : (s.state==='custom' ? 'Custom map' : 'Unknown map')));
  document.getElementById('currentName').textContent = name;

  const labels = {stock:'stock map', custom:'custom map', unknown:'unrecognised', missing:'no slot file'};
  const badge = document.getElementById('slotBadge');
  badge.className = 'badge b-' + ({stock:'stock',custom:'custom',unknown:'unknown',missing:'missing'}[s.state]||'unknown');
  badge.textContent = labels[s.state] || s.state;

  const bits = [];
  if (s.state==='custom') bits.push('swap active');
  if (s.size) bits.push(fmtSize(s.size));
  if (s.modified) bits.push('changed ' + new Date(s.modified*1000).toLocaleString());
  bits.push(s.backup_exists ? 'backup ok' : 'no backup');
  document.getElementById('currentMeta').textContent = bits.join('  \u2022  ');
  document.getElementById('restoreBtn').disabled = !s.backup_exists || s.state==='stock';

  const ht = document.getElementById('heroThumb');
  if (s.map_id && s.state==='custom') {
    ht.innerHTML = `<img src="${prevUrl(s.map_id)}" onerror="this.remove()"><span class="ph">&#127918;</span>`;
  } else {
    ht.innerHTML = `<span class="ph">${s.state==='stock' ? '\u{1F3DF}\uFE0F' : '\u2753'}</span>`;
  }

  const q = (document.getElementById('search').value||'').toLowerCase();
  const onlyBox = document.getElementById('everyoneOnly');
  const commonSet = Array.isArray(COMMON) ? new Set(COMMON) : null;
  onlyBox.disabled = !commonSet;
  let maps = (STATE.maps||[]).filter(m => !q || m.name.toLowerCase().includes(q));
  if (onlyBox.checked && commonSet) maps = maps.filter(m => commonSet.has(m.key));
  const hasPeers = !!(PEERS && PEERS.length);
  document.getElementById('count').textContent = maps.length + ' map(s)'
    + (onlyBox.checked && commonSet ? (hasPeers ? ' \u00b7 everyone has' : ' \u00b7 no peers') : '');
  document.getElementById('grid').innerHTML = maps.map(m => {
    const active = s.map_id && s.map_id === m.id;
    return `<div class="card${active?' is-active':''}">
      <div class="thumb">
        <span class="ph">&#127918;</span>
        <img loading="lazy" src="${prevUrl(m.id)}" onerror="this.remove()">
      </div>
      <div class="body">
        <div class="name" title="${esc(m.name)}">${esc(m.name)}</div>
        <div class="meta"><span class="src">${esc(m.source)}</span><span class="muted">${fmtSize(m.size)}</span></div>
        <button class="primary" onclick="useMap('${m.id}')" ${active?'disabled':''}>${active?'Loaded':'Load into slot'}</button>
      </div></div>`;
  }).join('') || '<div class="muted">No maps found. Put .udk/.upk files in ~/maps, or subscribe on Steam Workshop.</div>';
}

async function useMap(id){
  const m = (STATE.maps||[]).find(x=>x.id===id); if(!m) return;
  if(!confirm(`Load "${m.name}" into the ${ARENA} slot?\n\nThen in game: Free Play -> arena "${ARENA}".`)) return;
  toast('Swapping map...');
  const j = await (await api('/api/use',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})})).json();
  toast(j.ok ? ('Loaded: ' + m.name) : ('Error: ' + (j.error||'failed')));
  load_state();
}
async function restore(){
  if(!confirm(`Restore the stock ${ARENA} map? Do this before playing online.`)) return;
  const j = await (await api('/api/restore',{method:'POST'})).json();
  toast(j.ok ? 'Stock map restored' : ('Error: ' + (j.error||'failed')));
  load_state();
}
async function quitApp(){
  if(!confirm('Stop rpmap?\n\nThe server will shut down and this tab can be closed.')) return;
  try{ await api('/api/quit', {method:'POST'}); }catch(e){}
  TIMERS.forEach(clearInterval);
  document.body.innerHTML =
    '<div style="display:flex;align-items:center;justify-content:center;height:100vh;' +
    'color:#c9d1d9;font:15px system-ui,Segoe UI,Roboto,sans-serif;text-align:center">' +
    '<div><div style="font-size:34px">&#128075;</div>' +
    '<h2 style="color:#f0f6fc;margin:.3em 0">rpmap stopped</h2>' +
    '<p style="color:#8b949e">The server is off. You can close this tab.</p></div></div>';
}

let SESSION = null;
async function loadSession(){
  try{ SESSION = await (await api('/api/session')).json(); }
  catch(e){ SESSION = null; return; }
  const el = document.getElementById('mySecret');
  if(el) el.textContent = SESSION.secret || '-';
  const vl = document.getElementById('verLabel');
  if(vl) vl.textContent = 'v' + (SESSION.version || '?');
}
async function copySecret(){
  if(!SESSION || !SESSION.secret) return;
  try{ await navigator.clipboard.writeText(SESSION.secret); toast('Session code copied'); }
  catch(e){ toast(SESSION.secret); }
}
async function joinSession(){
  const v = (document.getElementById('joinCode').value||'').trim();
  if(!v) return;
  const r = await (await api('/api/session',{method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({secret:v})})).json();
  if(r.ok){ toast('Session code saved'); document.getElementById('joinCode').value=''; loadSession(); loadPeers(); }
  else toast('Error: ' + (r.error||'failed'));
}
function syncBadge(s){
  if(s==='same') return '<span class="badge b-stock">in sync</span>';
  if(s==='different') return '<span class="badge b-custom">out of sync</span>';
  return '<span class="badge b-missing">unknown</span>';
}
function reachLabel(p){
  if(p.reach==='offline') return '<span class="badge b-missing">offline</span>';
  if(p.reach==='not-rpmap') return '<span class="badge b-unknown">online \u00b7 not rpmap</span>';
  if(p.reach==='no-inventory') return '<span class="badge b-custom">rpmap \u00b7 no map list</span>';
  return '<span class="badge b-missing">unreachable</span>';
}
function peerRow(p, isSelf){
  const dot = `<span class="dot ${p.online?'on':'off'}"></span>`;
  const slot = p.slot || {};
  const map = !p.rpmap && !isSelf ? reachLabel(p)
            : slot.map_name ? esc(slot.map_name)
            : slot.state==='stock' ? 'Stock map'
            : slot.state ? esc(slot.state) : '\u2014';
  const size = slot.size ? fmtSize(slot.size) : '';
  const hash = p.sha256 ? `<code>${esc(String(p.sha256).slice(0,10))}</code>` : '';
  let verCell = '';
  if(p.version){
    if(!isSelf && SELF_VER && p.version !== SELF_VER){
      verCell = `<span style="color:var(--warn)" title="Peer runs v${esc(p.version)}; you run v${esc(SELF_VER)}">v${esc(p.version)} \u26a0</span>`;
    } else {
      verCell = `<span class="muted">v${esc(p.version)}</span>`;
    }
  }
  let actions = isSelf ? '' : syncBadge(p.in_sync);
  if(!isSelf && p.rpmap){
    const id = esc(p.peer_id||'');
    actions += ` <button onclick="syncFrom('${id}')" title="Load their map here">Switch me</button>`
             + ` <button onclick="syncTo('${id}')" title="Load your map there">Set theirs</button>`;
  }
  return `<tr>
    <td>${dot}${esc(p.name||'?')}${isSelf?' <span class="muted">(you)</span>':''}</td>
    <td class="muted">${esc(p.ip||'')}</td>
    <td>${verCell}</td>
    <td>${map}</td>
    <td class="muted">${size}</td>
    <td>${hash}</td>
    <td style="text-align:right;white-space:nowrap">${actions}</td>
  </tr>`;
}
let PEERS = null;
let SELF_VER = '';
async function loadPeers(){
  let P;
  try{ P = await (await api('/api/peers')).json(); }
  catch(e){ document.getElementById('peersStatus').textContent = 'server unreachable'; return; }
  PEERS = P.peers || [];
  const self = P.self || {};
  SELF_VER = self.version || '';
  COMMON = Array.isArray(P.common) ? P.common : null;
  const rows = [ peerRow({name:self.name, ip:self.ip, online:true, slot:self.slot,
                          sha256:self.sha256, version:self.version, in_sync:'same'}, true) ];
  PEERS.forEach(p => rows.push(peerRow(p, false)));
  document.getElementById('peerTable').innerHTML =
    '<tr><th>Player</th><th>IP</th><th>Version</th><th>Loaded map</th><th>Size</th><th>Hash</th><th></th></tr>'
    + rows.join('');
  const n = PEERS.length;
  const reachable = PEERS.filter(p => p.rpmap).length;
  const same = PEERS.filter(p => p.in_sync==='same').length;
  const vdiff = PEERS.filter(p => p.version && SELF_VER && p.version !== SELF_VER).length;
  document.getElementById('peersEmpty').style.display = n ? 'none' : 'block';
  let status;
  if(!n) status = 'no peers';
  else if(!reachable) status = 'no reachable peers';
  else status = same + '/' + reachable + ' in sync'
    + (n > reachable ? ' \u00b7 ' + (n - reachable) + ' unreachable' : '');
  if(vdiff) status += ' \u00b7 ' + vdiff + ' on a different rpmap version';
  if(COMMON) status += ' \u00b7 ' + COMMON.length + (n ? ' map(s) everyone has' : ' map(s) (all yours)');
  document.getElementById('peersStatus').textContent = status;
  render();   // refresh the "everyone has" filter availability/count
}
function peerTarget(p){ return {ip:p.ip, port:p.port, name:p.name}; }
function findPeer(id){ return (PEERS||[]).find(x => x.peer_id===id); }
async function doSync(mode, targets){
  if(!targets.length){ toast('No reachable peer running rpmap'); return; }
  toast(mode==='push' ? 'Switching peers...' : 'Switching this machine...');
  let r;
  try{
    r = await (await api('/api/sync',{method:'POST',headers:{'Content-Type':'application/json'},
             body:JSON.stringify({mode, peers:targets})})).json();
  }catch(e){ toast('Sync failed'); return; }
  const res = r.results || [];
  const bad = res.filter(x => !x.ok);
  if(bad.length){
    const b = bad[0];
    const who = b.peer || 'peer';
    let msg;
    if(b.error === 'not installed'){
      msg = (mode === 'pull')
        ? "You don't have this map (get it from the Workshop first)"
        : who + " doesn't have the map (needs a Workshop download)";
    } else if(b.error === 'version mismatch'){
      msg = 'Map content differs on ' + who + ' \u2014 not swapped';
    } else if(b.error === 'peer is on the stock map'){
      msg = who + ' is on the stock map';
    } else {
      msg = (b.peer ? who + ': ' : '') + (b.error || 'sync failed');
    }
    toastLink(msg, b.url);
  } else {
    toast(mode==='push' ? ('Pushed to ' + res.length + ' peer(s)')
                        : ('Switched to ' + ((res[0]&&res[0].map_name)||'their map')));
  }
  load_state(); loadPeers();
}
function syncFrom(id){ const p=findPeer(id); if(p) doSync('pull', [peerTarget(p)]); }
function syncTo(id){ const p=findPeer(id); if(p) doSync('push', [peerTarget(p)]); }
function syncAll(){ doSync('push', (PEERS||[]).filter(p=>p.rpmap).map(peerTarget)); }

load_state(); loadSession(); loadPeers();
TIMERS.push(setInterval(load_state, 4000));
TIMERS.push(setInterval(loadPeers, 8000));
</script>
</body></html>
"""


def make_handler(engine: dict):
    cooked: Path = engine["cooked"]
    slot: str = engine["slot"]
    token: str = engine["token"]
    host: str = engine["host"]
    ident: dict = engine.get("ident") or {}
    port: int = int(engine.get("port") or DEFAULT_PORT)
    dev: bool = bool(engine.get("dev"))

    allowed_hosts = {"127.0.0.1", "localhost", "::1", host}
    for _ip in (ident.get("ips") or []):
        allowed_hosts.add(_ip)
    if ident.get("dns"):
        allowed_hosts.add(ident["dns"])

    class Handler(BaseHTTPRequestHandler):
        server_version = "rpmap"

        def log_message(self, *a):
            pass

        def _send(self, body: bytes, ctype: str, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass          # browser navigated away / closed the tab mid-response

        def _json(self, obj, code=200):
            self._send(json.dumps(obj).encode(), "application/json", code)

        def _authed(self, q) -> bool:
            if q.get("token", [""])[0] == token:
                return True
            secret = engine.get("secret") or ""
            auth = self.headers.get("Authorization") or ""
            if secret and auth.startswith("Bearer "):
                return hmac.compare_digest(auth[7:].strip(), secret)
            return False

        def _source_ok(self) -> bool:
            try:
                addr = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return False
            if addr.is_loopback:
                return True
            if dev and addr.is_private:
                return True
            return addr in TAILNET

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").split(":")[0] in allowed_hosts

        def _local_ok(self) -> bool:
            """True only for a loopback client - used to gate quitting.

            A tailnet peer can reach the API, but must never be able to stop
            this machine's server.
            """
            try:
                return ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:
                return False

        def do_GET(self):
            if not self._source_ok():
                return self._json({"error": "forbidden source"}, 403)
            if not self._host_ok():
                return self._json({"error": "bad host"}, 403)
            u = urlparse(self.path)
            q = parse_qs(u.query)

            if u.path == "/":
                return self._send(PAGE.encode(), "text/html; charset=utf-8")

            if not u.path.startswith("/api/"):
                return self._json({"error": "not found"}, 404)
            if not self._authed(q):
                return self._json({"error": "bad token"}, 403)

            if u.path == "/api/state":
                return self._json({"cooked": str(cooked),
                                   "maps": [dict(m, key=map_key(m)) for m in list_maps(cooked)],
                                   "slot": slot_state(cooked, slot),
                                   "peer": {"peer_id": ident.get("peer_id"),
                                            "name": ident.get("name")}})

            if u.path == "/api/maps":
                return self._json({"maps": map_inventory(cooked)})

            if u.path == "/api/capabilities":
                st = slot_state(cooked, slot)
                return self._json({"product": PRODUCT, "version": APP_VERSION,
                                   "peer_id": ident.get("peer_id"), "name": ident.get("name"),
                                   "port": port,
                                   "slot": {"slot": st["slot"], "state": st["state"],
                                            "map_name": st["map_name"], "size": st["size"],
                                            "sha256": st.get("sha256") or None,
                                            "workshop_id": st.get("workshop_id")}})

            if u.path == "/api/session":
                return self._json({"peer_id": ident.get("peer_id"), "name": ident.get("name"),
                                   "port": port, "version": APP_VERSION,
                                   "secret": engine.get("secret") or ""})

            if u.path == "/api/preview":
                mid = q.get("id", [""])[0]
                m = next((x for x in list_maps(cooked) if x["id"] == mid), None)
                if m:
                    got = get_preview_bytes(m)
                    if got:
                        return self._send(got[0], got[1])
                return self._json({"error": "no preview"}, 404)

            if u.path == "/api/peers":
                return self._json(gather_peers(cooked, slot, engine.get("cfg") or {}, ident))

            return self._json({"error": "not found"}, 404)

        def do_POST(self):
            if not self._source_ok():
                return self._json({"error": "forbidden source"}, 403)
            if not self._host_ok():
                return self._json({"error": "bad host"}, 403)
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._authed(q):
                return self._json({"error": "bad token"}, 403)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                payload = {}
            try:
                if u.path == "/api/use":
                    mp = next((m for m in list_maps(cooked) if m["id"] == payload.get("id")), None)
                    if not mp:
                        return self._json({"error": "unknown map id"}, 400)
                    return self._json(use_map(cooked, slot, Path(mp["path"])))
                if u.path == "/api/restore":
                    return self._json(restore_slot(cooked, slot))
                if u.path == "/api/quit":
                    if not self._local_ok():
                        return self._json({"error": "local only"}, 403)
                    self._json({"ok": True})
                    quit_fn = engine.get("request_quit")
                    if quit_fn:
                        threading.Thread(target=quit_fn, daemon=True).start()
                    return
                if u.path == "/api/session":
                    new = str(payload.get("secret") or "").strip() or new_secret()
                    engine["secret"] = new
                    engine.setdefault("cfg", {})["session_secret"] = new
                    save_config(engine["cfg"])
                    return self._json({"ok": True, "secret": new})
                if u.path == "/api/peer/switch":
                    return self._json(peer_switch(cooked, slot, payload))
                if u.path == "/api/sync":
                    return self._json(sync_action(cooked, slot, engine.get("cfg") or {},
                                                  str(payload.get("mode") or "push"),
                                                  payload.get("peers") or []))
            except Exception as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"error": "not found"}, 404)

    return Handler


def _find_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return preferred


class _HttpServer(ThreadingHTTPServer):
    """Threaded HTTP server that stays quiet when a client just goes away."""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def open_browser(url: str) -> None:
    """Open ``url`` in the default browser.

    PyInstaller puts its bundled libs on ``LD_LIBRARY_PATH``; if a desktop
    opener (kde-open/xdg-open) inherits that it can pick up a bundled libssl
    that clashes with the system libcurl. So launch the opener ourselves with a
    cleaned environment on Linux.
    """
    if os.name != "nt":
        env = {k: v for k, v in os.environ.items()
               if k not in ("LD_LIBRARY_PATH", "LD_PRELOAD")}
        opener = shutil.which("xdg-open") or shutil.which("kde-open5") or shutil.which("kde-open")
        if opener:
            try:
                subprocess.Popen([opener, url], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                pass
    webbrowser.open(url)


def _ui_host(host: str | None) -> str:
    """A concrete address to reach a server bound to ``host`` from this machine."""
    host = str(host or "")
    return "127.0.0.1" if host in ("", "0.0.0.0", "::") else host


def running_instance() -> dict | None:
    """The live rpmap UI's runtime record, or None.

    A record counts as live only when its pid is still running *and* the server
    answers ``/api/capabilities`` - so a stale file (crash, reboot) is ignored.
    """
    rec = read_runtime()
    pid = rec.get("pid")
    port = rec.get("port")
    if not pid or not port or not pid_alive(int(pid)):
        return None
    token = quote(str(rec.get("token") or ""), safe="")
    url = (f"http://{_ui_host(rec.get('host'))}:{int(port)}"
           f"/api/capabilities?token={token}")
    try:
        with urllib.request.urlopen(url, timeout=1.0) as r:
            caps = json.loads(r.read())
    except Exception:
        return None
    return rec if caps.get("product") == PRODUCT else None


def stop_instance() -> bool:
    """Ask a running rpmap UI to quit; fall back to SIGTERM. False if none."""
    rec = read_runtime()
    pid = rec.get("pid")
    if not pid or not pid_alive(int(pid)):
        return False
    port, token = rec.get("port"), str(rec.get("token") or "")
    if port:
        url = (f"http://{_ui_host(rec.get('host'))}:{int(port)}"
               f"/api/quit?token={quote(token, safe='')}")
        req = urllib.request.Request(url, data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=2.0) as r:
                if json.loads(r.read()).get("ok"):
                    return True
        except Exception:
            pass
    try:                     # last resort; on Windows this skips restore-on-exit
        os.kill(int(pid), signal.SIGTERM)
        return True
    except OSError:
        return False


def ui_url(rec: dict) -> str:
    return f"http://{_ui_host(rec.get('host'))}:{int(rec['port'])}/?token={rec.get('token') or ''}"


def _install_stop_signals(on_stop=None) -> None:
    """Turn SIGTERM/SIGHUP into a clean shutdown so on-exit work still runs.

    Without a window, Ctrl+C already raises KeyboardInterrupt and closing the
    terminal sends SIGHUP (which would otherwise kill the process without
    running the ``finally``). With a window the main thread is inside Tk's loop,
    so we run ``on_stop`` (sets the quit event) instead of raising into the GUI.
    """
    def _stop(signum, frame):
        if on_stop is not None:
            on_stop()
        else:
            raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass


def _restore_on_exit(cooked: Path, slot: str) -> None:
    """Put the stock map back when the program exits cleanly (opt-in)."""
    try:
        st = slot_state(cooked, slot)
        if st["state"] in ("custom", "unknown") and st["backup_exists"]:
            restore_slot(cooked, slot)
            arena = SLOT_INFO.get(slot, {}).get("arena", slot)
            print(f"restored the stock {arena} map on exit", flush=True)
    except Exception as e:
        print(f"could not restore the stock map on exit: {e}", file=sys.stderr)


def run_server(cooked: Path, slot: str, host: str, port: int, auto_open: bool,
               secret: str | None = None, dev: bool = False,
               restore_on_exit: bool = False, window: bool = True) -> int:
    cfg = load_config()
    if secret:
        cfg["session_secret"] = secret
    if not cfg.get("session_secret"):
        cfg["session_secret"] = new_secret()
    if not cfg.get("port"):
        cfg["port"] = port
    save_config(cfg)
    ident = local_identity(cfg)
    token = secrets.token_urlsafe(16)
    actual = _find_port(port) if host == "127.0.0.1" else port
    engine = {"cooked": cooked, "slot": slot, "token": token, "host": host,
              "secret": cfg["session_secret"], "cfg": cfg, "ident": ident,
              "dev": dev, "port": actual}
    httpd = _HttpServer((host, actual), make_handler(engine))
    write_runtime({"pid": os.getpid(), "port": actual, "host": host,
                   "token": token, "version": APP_VERSION,
                   "started": int(time.time())})
    url = f"http://{_ui_host(host)}:{actual}/?token={token}"
    print(f"rpmap UI  ->  {url}", flush=True)
    print(f"slot: {SLOTS[slot][0]}   cooked: {cooked}", flush=True)
    print(f"peer: {ident['name']} ({ident['peer_id']})", flush=True)
    print(f"session: {cfg['session_secret']}   <- share with peers / paste in the UI", flush=True)
    if host == "127.0.0.1":
        print("hint: for peer sync run:  serve --host <tailscale-ip>", flush=True)
    else:
        print(f"note: bound to {host}; peers reach this on port {actual}", flush=True)
    print("quit:  the UI's Quit button, the status window, or `rpmap stop`", flush=True)
    if auto_open:
        threading.Thread(target=lambda: open_browser(url), daemon=True).start()

    if window and rpmap_window.available():
        _run_with_window(cooked, slot, httpd, engine, url)
    else:
        engine["request_quit"] = httpd.shutdown
        _install_stop_signals()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass

    clear_runtime(os.getpid())
    if restore_on_exit:
        _restore_on_exit(cooked, slot)
    httpd.server_close()
    print("bye", flush=True)
    return 0


def _run_with_window(cooked: Path, slot: str, httpd, engine: dict, url: str) -> None:
    """Serve from a background thread while the status window owns the main one.

    Tk must run its loop in the main thread, so the HTTP server moves to a
    daemon thread. ``_shutdown`` is the single quit entry point (window button,
    the window's close button, ``/api/quit`` or a signal): it stops the server
    and tells the window to close. Cleanup happens in the caller.
    """
    quit_event = threading.Event()

    def _shutdown():
        httpd.shutdown()
        quit_event.set()

    def _status() -> tuple[str, str]:
        st = slot_state(cooked, slot)
        arena = SLOT_INFO.get(slot, {}).get("arena", slot)
        if st["state"] == "stock":
            what = "stock map"
        elif st.get("map_name"):
            what = st["map_name"]
        else:
            what = {"custom": "custom map", "missing": "slot file missing",
                    "unknown": "unrecognised map"}.get(st["state"], st["state"])
        return arena, what

    def _restore() -> None:
        try:
            restore_slot(cooked, slot)
        except Exception as e:
            print(f"restore failed: {e}", file=sys.stderr, flush=True)

    engine["request_quit"] = _shutdown
    _install_stop_signals(_shutdown)
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    print("window: showing the rpmap status window (use --no-window to skip)", flush=True)
    try:
        rpmap_window.run(version=APP_VERSION, status_fn=_status,
                         on_open=lambda: open_browser(url),
                         on_restore=_restore, on_quit=_shutdown,
                         quit_event=quit_event)
    except Exception as e:
        print(f"could not show the status window ({e}); server keeps running",
              file=sys.stderr, flush=True)
        quit_event.set()
    httpd.shutdown()                    # idempotent: the loop above already ran
    server_thread.join(timeout=5)
