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

import rpmap_lan
import rpmap_log
import rpmap_update as update
import rpmap_window
from rpmap_config import (APP_VERSION, AUTO_UPDATE_MODES, DEFAULT_PORT,
                          GITHUB_RELEASES_PAGE, MODE_LAN, MODE_TAILSCALE,
                          NETWORK_MODES, PRODUCT, SLOTS, SLOT_INFO, TAILNET,
                          UPDATE_AUTO, UPDATE_CHECK, auto_update,
                          auto_update_setting, clear_runtime, icon_data_uri,
                          load_config, network_mode, network_mode_setting,
                          new_secret, pid_alive, read_runtime, save_config,
                          write_runtime)
from rpmap_maps import (get_preview_bytes, list_maps, map_inventory, map_key,
                        restore_slot, slot_state, use_map)
from rpmap_peers import (gather_peers, known_peers, local_identity, local_ipv4s,
                         peer_switch, sync_action, tailscale_installed,
                         tailscale_self)


# --------------------------------------------------------------------------- #
# Network mode (Tailscale vs. plain local network / L2 VPN)
# --------------------------------------------------------------------------- #

def _engine_mode(engine: dict) -> str:
    """The live mode for this run (kept in the engine; falls back to the config)."""
    return engine.get("network_mode") or network_mode(engine.get("cfg") or {})


def _network_status(engine: dict) -> dict:
    """The connection mode plus enough detection for the UI to explain it.

    ``needs_onboarding`` is deliberately Tailscale-first: a machine with Tailscale
    installed is never interrupted with the chooser - only someone without it is
    asked, which is exactly who might need the LAN path.
    """
    cfg = engine.get("cfg") or {}
    setting = network_mode_setting(cfg)
    mode = _engine_mode(engine)
    installed = tailscale_installed()
    ts_ip = None
    if mode != MODE_LAN and installed:   # no subprocess in LAN mode / when absent
        ts_ip = (tailscale_self().get("ips") or [None])[0]
    lan_ips = local_ipv4s()
    return {
        "mode": mode,
        "configured": setting is not None,
        "modes": list(NETWORK_MODES),
        "tailscale_installed": installed,
        "tailscale_available": bool(ts_ip),
        "tailnet_ip": ts_ip,
        "lan_ip": lan_ips[0] if lan_ips else None,
        # Tailscale-first: only a machine with no Tailscale at all is asked.
        "needs_onboarding": setting is None and not installed,
    }


def _network_set(engine: dict, payload: dict) -> dict:
    mode = str(payload.get("mode") or "").strip().lower()
    if mode not in NETWORK_MODES:
        return {"ok": False, "error": "mode must be 'tailscale' or 'lan'"}
    cfg = engine.setdefault("cfg", {})
    cfg["network_mode"] = mode
    save_config(cfg)
    engine["network_mode"] = mode
    engine["ident"] = local_identity(cfg)     # refresh IPs for the new mode
    return {"ok": True, "network": _network_status(engine)}


def _peers_manual_set(engine: dict, payload: dict) -> dict:
    """Add/update or remove a manual peer (the local-network roster)."""
    cfg = engine.setdefault("cfg", {})
    manual = [m for m in (cfg.get("manual_peers") or []) if isinstance(m, dict)]
    ip = str(payload.get("ip") or "").strip()
    name = str(payload.get("name") or "").strip()
    if payload.get("remove"):
        manual = [m for m in manual if str(m.get("ip") or "").strip() != ip]
    else:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return {"ok": False, "error": "enter a valid IP address"}
        existing = next((m for m in manual if str(m.get("ip") or "").strip() == ip), None)
        if existing is not None:
            if name:
                existing["name"] = name
        else:
            entry = {"ip": ip}
            if name:
                entry["name"] = name
            manual.append(entry)
    cfg["manual_peers"] = manual
    save_config(cfg)
    return {"ok": True, "manual_peers": manual}


# --------------------------------------------------------------------------- #
# Updates (opt-in self-update consent + a manual check/apply control)
# --------------------------------------------------------------------------- #

UPDATE_CACHE_TTL = 60.0          # seconds to reuse a GitHub release check


def _update_status(engine: dict, refresh: bool = False) -> dict:
    """The update preference plus (cached) release info for the UI.

    ``needs_onboarding`` is true only on a *packaged* build - the only place a
    self-update can happen - so source checkouts never get the trust prompt.
    A release check is a network call, so it only happens when the user asks
    (``refresh``) or has opted into checking/auto-installing on startup;
    otherwise the last cached result is reported (``checked`` says whether
    there is one).
    """
    cfg = engine.get("cfg") or {}
    setting = auto_update_setting(cfg)          # "off"/"check"/"auto" or None
    mode = auto_update(cfg)                     # effective (defaults to "off")
    can_self_update = update._update_target() is not None

    cache = engine.get("update_cache")
    fresh = bool(cache) and (time.time() - cache.get("at", 0)) < UPDATE_CACHE_TTL
    if refresh or (mode in (UPDATE_CHECK, UPDATE_AUTO) and not fresh):
        cache = engine["update_cache"] = {"at": time.time(),
                                          "info": update.check_for_update()}
    return {
        "current": APP_VERSION,
        "auto_update": setting,                 # what was chosen (None = unset)
        "mode": mode,                           # effective: off / check / auto
        "modes": list(AUTO_UPDATE_MODES),
        "configured": setting is not None,
        "can_self_update": can_self_update,
        "releases_url": GITHUB_RELEASES_PAGE,
        "needs_onboarding": setting is None and can_self_update,
        "checked": cache is not None,
        "info": cache.get("info") if cache else None,
    }


def _update_set(engine: dict, payload: dict) -> dict:
    mode = str(payload.get("mode") or "").strip().lower()
    if mode not in AUTO_UPDATE_MODES:
        return {"ok": False, "error": "mode must be 'off', 'check' or 'auto'"}
    cfg = engine.setdefault("cfg", {})
    cfg["auto_update"] = mode
    save_config(cfg)
    return {"ok": True, "update": _update_status(engine)}


def _update_apply(engine: dict) -> dict:
    """Download and install a newer release, then restart. Packaged builds only.

    ``update.apply_update`` replaces the running build and terminates this
    process, so it runs on a daemon thread *after* the reply is sent.
    """
    if update._update_target() is None:
        return {"ok": False, "applying": False,
                "error": f"this build can't self-update - download it from "
                         f"{GITHUB_RELEASES_PAGE}"}
    info = update.check_for_update()
    if not info:
        return {"ok": False, "applying": False, "error": "no newer release found"}
    reason = update._update_refusal(info)
    if reason:
        return {"ok": False, "applying": False,
                "error": f"{reason} - download it from {GITHUB_RELEASES_PAGE}"}
    threading.Thread(target=update.apply_update, args=(info,), daemon=True).start()
    return {"ok": True, "applying": True, "tag": info.get("tag"),
            "version": info.get("version")}


def _whats_new_status(engine: dict, latest: bool = False) -> dict:
    """Release notes between the last run's version and the running one.

    ``latest`` (the Updates-panel link) ignores the last-run version and returns
    the newest release's notes. Only packaged builds - the ones that can
    actually be updated - show this; the notes are fetched lazily and cached for
    the run. A fetch failure sets ``error`` so the UI can link out instead.

    The automatic popup is announced **once per run** - ``show`` is true on the
    first call after an update and false afterwards - so an update-mode change
    (or a page reload) doesn't pop it up again; the manual reopen uses
    ``latest`` and always reports the notes.
    """
    packaged = update._update_target() is not None
    prev = engine.get("prev_version")
    status = {
        "current": APP_VERSION,
        "from": prev,                           # last-seen version (None = first run)
        "to": APP_VERSION,
        "packaged": packaged,
        "releases_url": GITHUB_RELEASES_PAGE,
        "entries": [],
        "error": None,
        "show": packaged and (True if latest else bool(prev)),
    }
    if not packaged:
        return status                       # source checkouts never announce updates

    key = "whats_new_latest" if latest else "whats_new"
    cached = engine.get(key)
    if cached is None:
        if latest:
            entry = update.latest_release_notes()
            entries = [entry] if entry else None
        elif prev:
            entries = update.release_notes(prev, APP_VERSION)
        else:
            entries = []                    # first run: nothing to compare against
        if entries is None:                 # couldn't reach GitHub
            status["error"] = "couldn't reach GitHub for the release notes"
            return status                   # don't cache a transient failure
        cached = engine[key] = {"entries": entries}
    status["entries"] = cached["entries"]
    if not latest:
        # Announce once: only the first request after an update reports show.
        status["show"] = status["show"] and not engine.get("whats_new_consumed")
        if status["show"]:
            engine["whats_new_consumed"] = True
    return status


# --------------------------------------------------------------------------- #
# LAN discovery relay (started/stopped from the UI; one per machine)
# --------------------------------------------------------------------------- #

def _lan_status(engine: dict) -> dict:
    relay = engine.get("lan")
    available = _engine_mode(engine) != MODE_LAN
    if not relay:
        return {"running": False, "error": None, "available": available}
    return {
        "running": bool(relay.running),
        "mode": relay.mode,
        "host": relay.host,
        "peers": list(relay.peers or []),
        "relay_port": relay.relay_port,
        "inject": relay.inject_mode,
        "error": relay.error,
        "available": available,
    }


def _lan_start(engine: dict, payload: dict) -> dict:
    relay = engine.get("lan")
    if relay and relay.running:
        return {"ok": False, "error": "relay already running", "lan": _lan_status(engine)}

    mode = str(payload.get("mode") or "relay")   # symmetric is the default
    if mode not in ("host", "join", "relay"):
        return {"ok": False, "error": "mode must be 'host', 'join' or 'relay'"}

    token = engine.get("secret") or ""
    if not token:
        return {"ok": False, "error": "no session code set"}

    host = (payload.get("host") or "").strip() or None
    peers = None
    if mode in ("host", "relay"):
        peers = rpmap_lan.resolve_peers(engine.get("cfg") or {}, payload.get("peers"))
        if not peers:
            return {"ok": False, "error": "no peers online (need a tailnet peer)"}
    elif not host:
        return {"ok": False, "error": "join needs the host's tailnet IP"}

    try:
        relay_port = int(payload.get("relay_port") or rpmap_lan.DEFAULT_RELAY_PORT)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad relay port"}

    factory = engine.get("relay_factory") or rpmap_lan.Relay
    try:
        relay = factory(
            mode=mode, token=token, peers=peers, host=host,
            advertise=payload.get("advertise") or None,
            relay_port=relay_port, bind=payload.get("bind") or None,
            inject_mode="broadcast", lan_port=rpmap_lan.LAN_PORT,
            dev=bool(engine.get("dev")), log=engine.get("lan_log") or print)
        if not relay.start():
            return {"ok": False, "error": "relay already running"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    engine["lan"] = relay
    status = _lan_status(engine)
    if status.get("error"):
        return {"ok": False, "error": status["error"], "lan": status}
    return {"ok": True, "lan": status}


def _lan_stop(engine: dict) -> dict:
    relay = engine.get("lan")
    if relay and relay.running and not relay.stop():
        return {"ok": False, "error": "relay did not stop", "lan": _lan_status(engine)}
    return {"ok": True, "lan": _lan_status(engine)}


# --------------------------------------------------------------------------- #
# Web UI
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>rpmap - custom map loader</title>
<!--favicon-->
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

  /* connection-mode chooser */
  .modal-back{position:fixed;inset:0;background:#010409cc;display:flex;align-items:center;
              justify-content:center;z-index:20;padding:20px}
  .modal{background:var(--bg2);border:1px solid #30363d;border-radius:14px;max-width:640px;width:100%;
         padding:22px 24px;box-shadow:0 12px 40px #0009;max-height:90vh;overflow:auto}
  .modal h2{margin:0 0 4px;color:var(--white);font-size:19px}
  .opt{display:flex;gap:12px;align-items:flex-start;border:1px solid #30363d;border-radius:10px;
       padding:12px 14px;margin-top:10px;cursor:pointer}
  .opt:hover{background:var(--bg3)}
  .opt.on{border-color:var(--accent2);background:#23863622}
  .opt input{margin-top:3px;flex:0 0 auto}
  .opt .t{color:var(--white);font-weight:700}
  .opt .d{color:var(--fg2);font-size:12.5px;margin-top:3px}
  .rec{font-size:11px;padding:2px 7px;border-radius:6px;background:var(--accent);
       color:#fff;margin-left:6px;text-transform:uppercase;letter-spacing:.04em}
</style></head>
<body>
<header>
  <h1>rpmap</h1>
  <span class="muted" id="verLabel"></span>
  <span class="badge b-custom" id="updateBadge" style="display:none;cursor:pointer"
        onclick="scrollToUpdate()" title="A newer rpmap release is available">update available</span>
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
      <b>Connection</b>
      <span class="grow"></span>
      <span class="muted" id="networkStatus">checking...</span>
      <button onclick="openNetworkModal()">Change</button>
    </div>
    <div class="muted" id="networkHint" style="margin-top:6px"></div>
  </div>

  <div class="panel" id="updatePanel">
    <div class="row">
      <b>Updates</b>
      <span class="grow"></span>
      <span class="muted" id="updateStatus">checking...</span>
      <a id="releaseLink" style="display:none;color:#79c0ff" target="_blank"
         rel="noopener noreferrer">View release &#8599;</a>
      <button id="updateNowBtn" class="primary" style="display:none" onclick="applyUpdate()">Update now</button>
      <button id="whatsNewBtn" style="display:none" onclick="openWhatsNew(true)">What&#39;s new</button>
      <button onclick="checkUpdates()">Check for updates</button>
    </div>
    <div class="row" id="autoUpdateRow" style="margin-top:10px">
      <label class="muted" title="Don't check or install automatically (use the button above)">
        <input type="radio" name="updMode" value="off" onchange="setUpdateMode()"> Off</label>
      <label class="muted" title="Look for a newer release on startup and tell you, but never install it">
        <input type="radio" name="updMode" value="check" onchange="setUpdateMode()"> Check on startup</label>
      <label class="muted" title="Check for a newer release on startup and install it automatically">
        <input type="radio" name="updMode" value="auto" onchange="setUpdateMode()"> Install automatically</label>
      <span class="grow"></span>
      <span class="muted" id="autoUpdateNote"></span>
    </div>
    <div class="muted" id="updateHint" style="margin-top:6px"></div>
  </div>

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
    <div class="row" id="manualPeerRow" style="margin-top:10px;display:none">
      <input class="search" id="peerIp" placeholder="Peer IP (e.g. 192.168.1.20)">
      <input class="search" id="peerName" placeholder="Name (optional)" style="min-width:150px">
      <button onclick="addManualPeer()">Add peer</button>
      <span class="muted">Machines on your LAN / L2 VPN running rpmap.</span>
    </div>
    <table class="peers" id="peerTable" style="margin-top:10px"></table>
    <div class="muted" id="peersEmpty" style="margin-top:6px;display:none">
      No peers discovered. Start tailscaled, run both machines on the tailnet with the
      same session code, and reload.
    </div>
  </div>

  <div class="panel" id="lanPanel">
    <div class="row">
      <b>LAN discovery relay</b>
      <span class="grow"></span>
      <span class="muted" id="lanStatus">checking...</span>
    </div>
    <div class="row" style="margin-top:10px">
      <label class="muted"><input type="radio" name="lanMode" value="host" onchange="renderLan()"> I'm hosting</label>
      <label class="muted"><input type="radio" name="lanMode" value="join" onchange="renderLan()"> I'm joining</label>
      <label class="muted"><input type="radio" name="lanMode" value="relay" checked onchange="renderLan()"> Everyone (host &amp; join)</label>
      <input class="search" id="lanHost" placeholder="Host's tailnet IP (100.x.y.z)" style="display:none">
      <span class="grow"></span>
      <button class="primary" id="lanStart" onclick="startLan()">Start relay</button>
      <button class="danger" id="lanStop" onclick="stopLan()" disabled>Stop</button>
    </div>
    <div class="muted" id="lanHint" style="margin-top:6px">
      Relays Rocket League's in-game <b>Create LAN / Join LAN</b> discovery across the tailnet
      (everyone needs the same session code). It carries <b>only discovery</b> &mdash; the match
      itself runs over Tailscale; prefer a direct path over a DERP relay.
    </div>
  </div>

  <details class="panel" id="logPanel">
    <summary style="cursor:pointer;color:var(--white)"><b>Logs</b>
      <span class="muted" style="font-weight:normal"> &mdash; server output (updates, relay, errors)</span></summary>
    <div class="row" style="margin-top:10px">
      <span class="muted">Shown for this machine only. Newest at the bottom.</span>
      <span class="grow"></span>
      <button onclick="copyLog()">Copy</button>
      <button onclick="clearLog()">Clear</button>
    </div>
    <pre id="logPre" style="margin-top:10px;max-height:320px;overflow:auto;white-space:pre-wrap;
         background:#0b0f14;border:1px solid #30363d;border-radius:8px;padding:10px;
         font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--fg)"></pre>
  </details>
</main>
<div class="toast" id="toast"></div>

<div class="modal-back" id="netModal" style="display:none">
  <div class="modal">
    <h2>How are you connecting to the other players?</h2>
    <p class="muted" style="margin:6px 0 0">
      rpmap can reach your friends over <b>Tailscale</b> or straight over your own network.
      It only affects how peers are found and which addresses are accepted &mdash; swapping maps
      works the same either way. You can change this later in <b>Connection</b>.
    </p>
    <label class="opt" id="optTs" onclick="pickMode('tailscale')">
      <input type="radio" name="netMode" value="tailscale">
      <span>
        <span class="t">Tailscale<span class="rec">recommended</span></span>
        <span class="d">A Layer-3 mesh VPN. Rocket League joins by IP:port, so Tailscale's
          stable <code>100.x</code> address works directly &mdash; no LAN broadcast needed, and NAT
          traversal is automatic. It also powers the automatic player list. Already built into
          Bazzite, and the setup this tool is tested against.</span>
      </span>
    </label>
    <label class="opt" id="optLan" onclick="pickMode('lan')">
      <input type="radio" name="netMode" value="lan">
      <span>
        <span class="t">Local network / L2 VPN</span>
        <span class="d">For an actual LAN, or an L2 VPN such as ZeroTier or Hamachi. These
          emulate a Layer-2 network, so they need more setup and their broadcast/multicast can be
          inconsistent (MTU quirks, partial support) &mdash; and Hamachi is proprietary, caps free
          networks at 5 peers and is effectively unmaintained. Here the game's own LAN discovery
          is used directly, so the discovery relay isn't needed; you add peers by IP.</span>
      </span>
    </label>
    <div class="muted" id="netModalDetect" style="margin-top:10px"></div>
    <div class="row" style="justify-content:flex-end;margin-top:14px">
      <button onclick="closeNetworkModal(true)">Continue with Tailscale</button>
      <button class="primary" onclick="saveNetworkMode()">Save</button>
    </div>
  </div>
</div>

<div class="modal-back" id="updateModal" style="display:none">
  <div class="modal">
    <h2>Allow automatic updates?</h2>
    <p class="muted" style="margin:6px 0 0">
      rpmap can check GitHub for new releases and, if you want, install them automatically -
      replacing the app and restarting. Auto-install means <b>trusting the owner of the
      repository (<code>JThFr</code>)</b> to publish whatever they want. Downloads are
      <b>sha256-verified</b> and only accepted from this project&#39;s official GitHub releases.
      Checking for updates on its own carries no such risk, and you can change this any time
      under <b>Updates</b>.
    </p>
    <div class="muted" id="updateModalLink" style="margin-top:10px"></div>
    <div class="row" style="justify-content:flex-end;margin-top:14px">
      <button onclick="saveUpdateMode('off')">No, I&#39;ll check manually</button>
      <button onclick="saveUpdateMode('check')">Check on startup</button>
      <button class="primary" onclick="saveUpdateMode('auto')">Install automatically</button>
    </div>
  </div>
</div>

<div class="modal-back" id="whatsNewModal" style="display:none">
  <div class="modal">
    <h2 id="whatsNewTitle">What&#39;s new</h2>
    <div class="muted" id="whatsNewIntro" style="margin:6px 0 0"></div>
    <div id="whatsNewBody" style="margin-top:12px;max-height:52vh;overflow:auto"></div>
    <div class="row" style="justify-content:flex-end;margin-top:14px">
      <a id="whatsNewLink" target="_blank" rel="noopener noreferrer"
         style="color:#79c0ff;align-self:center;margin-right:auto">All releases &#8599;</a>
      <button class="primary" onclick="closeWhatsNew()">Got it</button>
    </div>
  </div>
</div>
<script>
let STATE = null;
let TIMERS = [];        // poll intervals, cleared when the user quits
let COMMON = null;      // map keys that every reachable peer has, or null
let ARENA = 'the arena';
let ARENA_NOTE = '';
let NETWORK = null;     // {mode, tailscale_available, tailnet_ip, lan_ip, ...}
let UPDATE = null;      // {current, auto_update, can_self_update, info, ...}
let WHATS_NEW = null;   // {current, from, to, entries, error, ...}
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
    a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer';
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
        <button class="primary useBtn" data-map-id="${esc(m.id)}" ${active?'disabled':''}>${active?'Loaded':'Load into slot'}</button>
      </div></div>`;
  }).join('') || '<div class="muted">No maps found. Put .udk/.upk files in ~/maps, or subscribe on Steam Workshop (Steam only; on Epic, download the map and drop it in ~/maps).</div>';
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
  stopLogPolling();
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
    actions += ` <button class="syncFromBtn" data-peer-id="${id}" title="Load their map here">Switch me</button>`
             + ` <button class="syncToBtn" data-peer-id="${id}" title="Load your map there">Set theirs</button>`;
  }
  if(!isSelf && p.manual){
    actions += ` <button class="peerRemoveBtn" data-peer-ip="${esc(p.ip||'')}" title="Remove this manual peer">Remove</button>`;
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
  document.getElementById('peersEmpty').innerHTML = (NETWORK && NETWORK.mode === 'lan')
    ? 'No peers yet. Add a peer\u2019s IP above (both machines need the same session code).'
    : 'No peers discovered. Start tailscaled, run both machines on the tailnet with the same session code, and reload.';
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
        ? (b.url ? "You don't have this map (get it from the Workshop first)"
                 : "You don't have this map yet")
        : who + " doesn't have the map" + (b.url ? " (needs a Workshop download)" : "");
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

let LAN = null;
function lanMode(){ const r = document.querySelector('input[name="lanMode"]:checked'); return r ? r.value : 'host'; }
function fmtLanPeers(p){ return (p && p.length) ? p.join(', ') : 'all online peers'; }
function renderLan(){
  const join = lanMode() === 'join';
  document.getElementById('lanHost').style.display = join ? '' : 'none';
  const L = LAN || {};
  const st = document.getElementById('lanStatus');
  if(L.running){
    const what = L.mode === 'join' ? 'joining ' + esc(L.host || '?')
      : L.mode === 'relay' ? 'everyone relays &rarr; ' + esc(fmtLanPeers(L.peers))
      : 'hosting &rarr; ' + esc(fmtLanPeers(L.peers));
    st.innerHTML = '<span class="badge b-stock">relaying</span> ' + what;
  } else if(L.error){
    st.innerHTML = '<span class="badge b-unknown">stopped</span> ' + esc(L.error);
  } else {
    st.innerHTML = '<span class="badge b-missing">idle</span>';
  }
  document.getElementById('lanStart').disabled = !!L.running;
  document.getElementById('lanStop').disabled = !L.running;
}
async function loadLan(){
  try{ LAN = await (await api('/api/lan')).json(); }catch(e){ return; }
  renderLan();
}
async function startLan(){
  const mode = lanMode();
  const host = (document.getElementById('lanHost').value || '').trim();
  if(mode === 'join' && !host){ toast('Enter the host tailnet IP'); return; }
  const r = await (await api('/api/lan/start', {method:'POST', headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({mode, host})})).json();
  if(r.ok) toast('LAN relay started'); else toast('Error: ' + (r.error || 'failed'));
  loadLan();
}
async function stopLan(){
  const r = await (await api('/api/lan/stop', {method:'POST'})).json();
  if(r.ok) toast('LAN relay stopped'); else toast('Error: ' + (r.error || 'failed'));
  loadLan();
}

// --- connection mode (Tailscale vs. local network) -------------------------
function modeLabel(m){ return m === 'lan' ? 'Local network / L2 VPN' : 'Tailscale'; }
function renderNetwork(){
  if(!NETWORK) return;
  const mode = NETWORK.mode;
  document.getElementById('networkStatus').innerHTML = '<b>' + esc(modeLabel(mode)) + '</b>';
  const hint = document.getElementById('networkHint');
  if(mode === 'lan'){
    const ip = NETWORK.lan_ip ? ' &middot; this machine: <code>' + esc(NETWORK.lan_ip) + '</code>' : '';
    hint.innerHTML = 'Using your own network directly. Rocket League\u2019s native LAN discovery works '
      + 'as-is here, so the relay panel is hidden.' + ip;
  } else if(NETWORK.tailscale_available){
    hint.innerHTML = 'Connecting over your tailnet'
      + (NETWORK.tailnet_ip ? ' &middot; this machine: <code>' + esc(NETWORK.tailnet_ip) + '</code>' : '') + '.';
  } else {
    hint.innerHTML = NETWORK.tailscale_installed
      ? 'Tailscale is selected but not running &mdash; start it with <code>sudo tailscale up</code>, or switch below.'
      : 'Tailscale is selected but not installed &mdash; install it, or switch below.';
  }
  document.getElementById('lanPanel').style.display = mode === 'lan' ? 'none' : '';
  document.getElementById('manualPeerRow').style.display = mode === 'lan' ? '' : 'none';
}
async function loadNetwork(){
  try{ NETWORK = await (await api('/api/network')).json(); }catch(e){ return; }
  renderNetwork();
}
function pickMode(mode){
  const r = document.querySelector('input[name="netMode"][value="' + mode + '"]');
  if(r) r.checked = true;
  document.getElementById('optTs').classList.toggle('on', mode === 'tailscale');
  document.getElementById('optLan').classList.toggle('on', mode === 'lan');
}
function openNetworkModal(){
  pickMode(NETWORK ? NETWORK.mode : 'tailscale');
  const det = document.getElementById('netModalDetect');
  if(NETWORK){
    det.innerHTML = NETWORK.tailscale_available
      ? 'Tailscale detected: <code>' + esc(NETWORK.tailnet_ip || '') + '</code>.'
      : (NETWORK.tailscale_installed
         ? 'Tailscale is installed but not connected \u2014 start it with <code>sudo tailscale up</code>.'
         : 'Tailscale not detected on this machine.');
    if(NETWORK.lan_ip) det.innerHTML += '  This machine\u2019s LAN address: <code>' + esc(NETWORK.lan_ip) + '</code>.';
  }
  document.getElementById('netModal').style.display = '';
}
function closeNetworkModal(useTailscale){
  document.getElementById('netModal').style.display = 'none';
  if(useTailscale) saveNetworkMode('tailscale');
}
async function saveNetworkMode(force){
  const box = document.querySelector('input[name="netMode"]:checked');
  const mode = force || (box && box.value) || 'tailscale';
  const r = await (await api('/api/network', {method:'POST', headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({mode})})).json();
  if(r.ok){
    NETWORK = r.network;
    document.getElementById('netModal').style.display = 'none';
    renderNetwork();
    toast('Connection: ' + modeLabel(mode));
    loadPeers(); loadLan();
    maybeOnboardUpdate().then(opened => { if(!opened) maybeShowWhatsNew(); });
  } else toast('Error: ' + (r.error || 'failed'));
}
async function addManualPeer(){
  const ip = (document.getElementById('peerIp').value || '').trim();
  const name = (document.getElementById('peerName').value || '').trim();
  if(!ip){ toast('Enter the peer IP'); return; }
  const r = await (await api('/api/peers/manual', {method:'POST', headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({ip, name})})).json();
  if(r.ok){
    document.getElementById('peerIp').value = '';
    document.getElementById('peerName').value = '';
    toast('Peer added'); loadPeers();
  } else toast('Error: ' + (r.error || 'failed'));
}
async function removeManualPeer(ip){
  const r = await (await api('/api/peers/manual', {method:'POST', headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({ip, remove:true})})).json();
  if(r.ok){ toast('Peer removed'); loadPeers(); } else toast('Error: ' + (r.error || 'failed'));
}

// --- updates (opt-in self-update) ------------------------------------------
function scrollToUpdate(){
  const p = document.getElementById('updatePanel');
  if(p) p.scrollIntoView({behavior:'smooth', block:'center'});
}
function renderUpdate(){
  if(!UPDATE) return;
  const info = UPDATE.info;
  const status = document.getElementById('updateStatus');
  if(!UPDATE.checked){
    status.innerHTML = '<span class="muted">not checked</span>';
  } else if(info){
    status.innerHTML = 'New version <b>' + esc(info.tag) + '</b> available';
  } else {
    status.innerHTML = '<span class="badge b-stock">up to date</span> v' + esc(UPDATE.current);
  }
  document.getElementById('updateNowBtn').style.display = (info && UPDATE.can_self_update) ? '' : 'none';
  document.getElementById('whatsNewBtn').style.display = UPDATE.can_self_update ? '' : 'none';
  const link = document.getElementById('releaseLink');
  link.style.display = info ? '' : 'none';
  if(info) link.href = info.url || UPDATE.releases_url;
  document.getElementById('updateBadge').style.display = info ? '' : 'none';

  const row = document.getElementById('autoUpdateRow');
  if(UPDATE.can_self_update){
    row.style.display = '';
    const radio = document.querySelector('input[name="updMode"][value="' + UPDATE.mode + '"]');
    if(radio) radio.checked = true;
    document.getElementById('autoUpdateNote').textContent =
      UPDATE.mode === 'auto' ? 'installs on startup'
      : UPDATE.mode === 'check' ? 'tells you, never installs' : 'manual only';
  } else {
    row.style.display = 'none';
  }

  const hint = document.getElementById('updateHint');
  if(!UPDATE.can_self_update){
    hint.innerHTML = 'This is a source build, so it can\u2019t replace itself. New versions come '
      + 'from the <a href="' + esc(UPDATE.releases_url) + '" target="_blank" '
      + 'rel="noopener noreferrer" style="color:#79c0ff">GitHub releases</a>.';
  } else if(info){
    hint.innerHTML = 'Downloads are sha256-verified and only accepted from this project\u2019s '
      + 'official GitHub releases.';
  } else if(!UPDATE.checked){
    hint.innerHTML = 'Click <b>Check for updates</b> to ask GitHub for the latest release, or '
      + 'pick a startup option below.';
  } else {
    hint.innerHTML = UPDATE.mode === 'auto'
      ? 'rpmap is up to date. New releases install automatically on startup.'
      : UPDATE.mode === 'check'
        ? 'rpmap is up to date. It will check again on startup and tell you.'
        : 'rpmap is up to date.';
  }
}
async function loadUpdate(){
  try{ UPDATE = await (await api('/api/update')).json(); }catch(e){ return null; }
  renderUpdate();
  return UPDATE;
}
async function checkUpdates(){
  toast('Checking for updates...');
  try{ UPDATE = await (await api('/api/update?refresh=1')).json(); }
  catch(e){ toast('Could not reach the server'); return; }
  renderUpdate();
  toast(UPDATE.info ? ('Update available: ' + UPDATE.info.tag) : 'rpmap is up to date');
}
async function setUpdateMode(){
  const box = document.querySelector('input[name="updMode"]:checked');
  await saveUpdateMode(box ? box.value : 'off');
}
async function saveUpdateMode(mode){
  document.getElementById('updateModal').style.display = 'none';
  const r = await (await api('/api/update/mode', {method:'POST', headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({mode})})).json();
  if(r.ok){
    UPDATE = r.update; renderUpdate();
    toast(mode === 'auto' ? 'Auto-updates on'
        : mode === 'check' ? 'Will check for updates on startup'
        : 'Auto-updates off \u2014 check manually');
    maybeShowWhatsNew();      // one modal at a time: news comes after consent
  } else {
    toast('Error: ' + (r.error || 'failed'));
    loadUpdate();
  }
}
function openUpdateModal(){
  const l = document.getElementById('updateModalLink');
  if(UPDATE) l.innerHTML = 'Releases: <a href="' + esc(UPDATE.releases_url) + '" target="_blank" '
    + 'rel="noopener noreferrer" style="color:#79c0ff">github.com/'
    + esc(String(UPDATE.releases_url).split('github.com/')[1] || '') + '</a>.';
  document.getElementById('updateModal').style.display = '';
}
async function maybeOnboardUpdate(){
  const u = await loadUpdate();
  if(u && u.local && u.needs_onboarding){ openUpdateModal(); return true; }
  return false;
}

// --- what's new (announced once after an update) ---------------------------
function linkify(text, parent){
  const re = /https?:\/\/[^\s)<>"]+/;
  let rest = String(text == null ? '' : text);
  while(rest){
    const m = rest.match(re);
    if(!m){ parent.appendChild(document.createTextNode(rest)); break; }
    parent.appendChild(document.createTextNode(rest.slice(0, m.index)));
    const a = document.createElement('a');
    a.href = m[0]; a.target = '_blank'; a.rel = 'noopener noreferrer';
    a.textContent = m[0]; a.style.color = '#79c0ff';
    parent.appendChild(a);
    rest = rest.slice(m.index + m[0].length);
  }
}
function whatsNewSection(entry){
  const sec = document.createElement('div');
  sec.style.margin = '0 0 14px';
  const head = document.createElement('div');
  head.style.cssText = 'color:var(--white);font-weight:700';
  const label = 'v' + entry.version
    + (entry.title && entry.title !== entry.tag ? '  \u2014  ' + entry.title : '');
  head.appendChild(document.createTextNode(label));
  const link = document.createElement('a');
  link.href = entry.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
  link.textContent = 'View release \u2197';
  link.style.cssText = 'color:#79c0ff;margin-left:8px;font-weight:400;font-size:12.5px';
  head.appendChild(link);
  sec.appendChild(head);
  // The body is remote Markdown: render it as text (never innerHTML) so a
  // malicious release can't inject markup into the page.
  String(entry.body || '').split('\n').forEach(line => {
    const t = line.trim();
    if(!t) return;
    const el = document.createElement('div');
    if(/^#{1,6}\s/.test(t)){
      el.style.cssText = 'color:var(--white);font-weight:600;margin-top:6px';
      linkify(t.replace(/^#{1,6}\s+/, ''), el);
    } else if(/^[*+-]\s/.test(t)){
      el.style.cssText = 'margin-left:14px';
      el.appendChild(document.createTextNode('\u2022 '));
      linkify(t.replace(/^[*+-]\s+/, ''), el);
    } else {
      el.style.fontSize = '12.5px'; el.style.color = 'var(--fg2)';
      linkify(t, el);
    }
    sec.appendChild(el);
  });
  return sec;
}
function renderWhatsNew(data){
  WHATS_NEW = data;
  const title = document.getElementById('whatsNewTitle');
  const intro = document.getElementById('whatsNewIntro');
  const body = document.getElementById('whatsNewBody');
  body.textContent = '';
  document.getElementById('whatsNewLink').href = data.releases_url || '#';
  if(data.error){
    title.textContent = "What's new in rpmap " + data.to;
    intro.textContent = '';
    const p = document.createElement('div');
    p.className = 'muted';
    p.textContent = 'Updated to v' + data.to
      + (data.from ? ' (from v' + data.from + ')' : '') + '. ' + data.error
      + ' \u2014 following the link opens the full release list.';
    body.appendChild(p);
    return;
  }
  if(!data.entries || !data.entries.length){
    title.textContent = "rpmap v" + data.to;
    intro.textContent = 'No release notes to show'
      + (data.from ? ' since v' + data.from : '') + '.';
    return;
  }
  title.textContent = "What's new in rpmap " + data.to;
  intro.textContent = data.from
    ? 'Updated from v' + data.from + ' to v' + data.to + '.'
    : 'The latest rpmap release.';
  data.entries.forEach(e => body.appendChild(whatsNewSection(e)));
}
async function openWhatsNew(manual){
  let data;
  try{ data = await (await api('/api/whatsnew' + (manual ? '?latest=1' : ''))).json(); }
  catch(e){ return; }
  if(!data || !data.packaged) return;
  if(!manual && !data.show) return;
  renderWhatsNew(data);
  document.getElementById('whatsNewModal').style.display = '';
}
function closeWhatsNew(){ document.getElementById('whatsNewModal').style.display = 'none'; }
function maybeShowWhatsNew(){ return openWhatsNew(false); }
async function applyUpdate(){
  if(!confirm('Update rpmap now?\n\nIt downloads the new version, replaces the app, and restarts. '
              + 'This page will lose its server.')) return;
  const btn = document.getElementById('updateNowBtn');
  btn.disabled = true; btn.textContent = 'Updating...';
  let r;
  try{ r = await (await api('/api/update/apply', {method:'POST'})).json(); }
  catch(e){ toast('Update request failed'); btn.disabled = false; btn.textContent = 'Update now'; return; }
  if(!r.ok){
    toast('Error: ' + (r.error || 'failed'));
    btn.disabled = false; btn.textContent = 'Update now';
    return;
  }
  document.getElementById('updateStatus').innerHTML =
    'Updating to <b>' + esc(r.tag || '') + '</b> and restarting...';
  TIMERS.forEach(clearInterval);
  toast('Updating and restarting \u2014 this page will stop responding. Reopen rpmap afterwards.', 9000);
}

// --- logs (the server's stdout/stderr, shown locally) ----------------------
let LOG_SEQ = 0;
let LOG_TIMER = null;
function appendLog(e){
  const pre = document.getElementById('logPre');
  const row = document.createElement('div');
  const ts = e.t ? new Date(e.t * 1000).toLocaleTimeString() : '';
  row.textContent = (ts ? ts + '  ' : '') + (e.msg || '');
  if(e.level === 'error') row.style.color = '#ff7b72';
  else if(e.level === 'warn') row.style.color = '#e3b341';
  pre.appendChild(row);
  while(pre.childElementCount > 500) pre.removeChild(pre.firstChild);
  pre.scrollTop = pre.scrollHeight;
}
async function loadLog(){
  let r;
  try{ r = await (await api('/api/log?after=' + LOG_SEQ)).json(); }
  catch(e){ return; }
  if(!r || r.error) return;
  (r.entries || []).forEach(appendLog);
  if(r.next) LOG_SEQ = r.next;
}
function startLogPolling(){
  if(LOG_TIMER) return;
  loadLog();
  LOG_TIMER = setInterval(loadLog, 3000);
}
function stopLogPolling(){ if(LOG_TIMER){ clearInterval(LOG_TIMER); LOG_TIMER = null; } }
function copyLog(){
  const text = Array.from(document.getElementById('logPre').children)
    .map(r => r.textContent).join('\n');
  navigator.clipboard.writeText(text).then(() => toast('Logs copied'),
                                           () => toast('Could not copy logs'));
}
function clearLog(){ document.getElementById('logPre').innerHTML = ''; }

// One delegated listener instead of inline handlers: ids travel as element
// data (HTML-escaped attributes) and are never interpolated into generated
// JS source - inline "onclick=\"fn('${id}')\"" patterns are an injection trap
// because HTML entities decode before the JS engine sees the string.
document.addEventListener('click', e => {
  const btn = e.target.closest('button');
  if(!btn) return;
  if(btn.classList.contains('useBtn')) useMap(btn.dataset.mapId);
  else if(btn.classList.contains('syncFromBtn')) syncFrom(btn.dataset.peerId);
  else if(btn.classList.contains('syncToBtn')) syncTo(btn.dataset.peerId);
  else if(btn.classList.contains('peerRemoveBtn')) removeManualPeer(btn.dataset.peerIp);
});

load_state(); loadSession(); loadLan();
loadNetwork().then(() => {
  renderNetwork();
  loadPeers();
  if(NETWORK && NETWORK.needs_onboarding) openNetworkModal();
  else maybeOnboardUpdate().then(opened => { if(!opened) maybeShowWhatsNew(); });
});
const logPanel = document.getElementById('logPanel');
if(logPanel) logPanel.addEventListener('toggle',
  () => logPanel.open ? startLogPolling() : stopLogPolling());
TIMERS.push(setInterval(load_state, 4000));
TIMERS.push(setInterval(loadPeers, 8000));
TIMERS.push(setInterval(loadLan, 5000));
TIMERS.push(setInterval(loadNetwork, 15000));
</script>
</body></html>
"""

# The favicon comes from the bundled/source icon (rpmap_config), not a pasted
# blob, so the artwork lives in one place (``assets/rpmap.png``).
_favicon = icon_data_uri()
PAGE = PAGE.replace(
    "<!--favicon-->",
    f'<link rel="icon" type="image/png" href="{_favicon}">' if _favicon else "")


def make_handler(engine: dict):
    cooked: Path = engine["cooked"]
    slot: str = engine["slot"]
    token: str = engine["token"]
    host: str = engine["host"]
    ident: dict = engine.get("ident") or {}
    port: int = int(engine.get("port") or DEFAULT_PORT)
    dev: bool = bool(engine.get("dev"))
    engine.setdefault("network_mode", network_mode(engine.get("cfg") or {}))

    def _ident() -> dict:
        """This run's identity (re-read live: switching mode re-derives it)."""
        return engine.get("ident") or ident

    # Host allow-list: loopback, the bound host, this machine's own addresses
    # (so a UI opened on a LAN/tailnet IP passes the Host check), and the
    # tailnet DNS name. Source gating below still decides who may actually talk.
    allowed_hosts = {"127.0.0.1", "localhost", "::1", host}
    for _ip in ((ident.get("ips") or []) + local_ipv4s()):
        allowed_hosts.add(str(_ip))
    if ident.get("dns"):
        allowed_hosts.add(ident["dns"])
    # Addresses that count as "this machine" for the local-only gates: the bound
    # host plus every address of ours (tailnet and LAN). A UI served on such an
    # address is reached by the local browser *from* it, not from 127.0.0.1.
    own_ips = {host} | {str(_ip) for _ip in (ident.get("ips") or [])} | set(local_ipv4s())

    class Handler(BaseHTTPRequestHandler):
        server_version = "rpmap"

        def log_message(self, *a):
            pass

        def _send(self, body: bytes, ctype: str, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # The UI token lives in the URL query, and previews are served
            # with a sniffed content type - so never let a browser guess the
            # type, and never let a link click leak the URL elsewhere. The CSP
            # is deliberately scoped: the page uses inline script/style, so
            # instead of pretending `script-src 'unsafe-inline'` helps, it
            # blocks the things that matter here - framing, <base> hijack and
            # form hijack.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "frame-ancestors 'none'; base-uri 'none'; "
                             "form-action 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass          # browser navigated away / closed the tab mid-response

        def _json(self, obj, code=200):
            self._send(json.dumps(obj).encode(), "application/json", code)

        def _authed(self, q) -> bool:
            if hmac.compare_digest(str(q.get("token", [""])[0]).encode(),
                                   token.encode()):
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
            if engine.get("network_mode") == MODE_LAN:
                return addr.is_private      # LAN / L2 VPN only (100.64/10 is not "private")
            return addr in TAILNET

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            if host in allowed_hosts:
                return True
            # A literal tailnet/LAN address is fine too: the source gate has
            # already decided whether this client may talk to us, and this only
            # needs to reject a foreign *name* (DNS-rebinding) or public IP.
            try:
                addr = ipaddress.ip_address(host)
            except ValueError:
                return False
            if addr.is_loopback:
                return True
            if engine.get("network_mode") == MODE_LAN:
                return addr.is_private
            return addr in TAILNET

        def _local_ok(self) -> bool:
            """True only for a client on this machine - used to gate quitting
            and rotating the session code.

            Loopback, plus this machine's own addresses (a UI served on the
            tailnet IP is reached by the local browser from that address). A
            remote peer can reach the API, but must never be able to stop this
            machine's server or replace its session code.
            """
            try:
                addr = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return False
            return addr.is_loopback or str(addr) in own_ips

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
                                   "peer": {"peer_id": _ident().get("peer_id"),
                                            "name": _ident().get("name")}})

            if u.path == "/api/maps":
                return self._json({"maps": map_inventory(cooked)})

            if u.path == "/api/capabilities":
                st = slot_state(cooked, slot)
                return self._json({"product": PRODUCT, "version": APP_VERSION,
                                   "peer_id": _ident().get("peer_id"),
                                   "name": _ident().get("name"),
                                   "port": port,
                                   "slot": {"slot": st["slot"], "state": st["state"],
                                            "map_name": st["map_name"], "size": st["size"],
                                            "sha256": st.get("sha256") or None,
                                            "workshop_id": st.get("workshop_id")}})

            if u.path == "/api/session":
                return self._json({"peer_id": _ident().get("peer_id"),
                                   "name": _ident().get("name"),
                                   "port": port, "version": APP_VERSION,
                                   "secret": engine.get("secret") or ""})

            if u.path == "/api/network":
                return self._json(_network_status(engine))

            if u.path == "/api/update":
                status = _update_status(engine, refresh=bool(q.get("refresh")))
                status["local"] = self._local_ok()
                return self._json(status)

            if u.path == "/api/whatsnew":
                return self._json(_whats_new_status(engine, latest=bool(q.get("latest"))))

            if u.path == "/api/log":
                if not self._local_ok():
                    return self._json({"error": "local only"}, 403)
                try:
                    after = int(q.get("after", ["0"])[0])
                except ValueError:
                    after = 0
                items, nxt = rpmap_log.entries(after)
                return self._json({"entries": items, "next": nxt})

            if u.path == "/api/preview":
                mid = q.get("id", [""])[0]
                m = next((x for x in list_maps(cooked) if x["id"] == mid), None)
                if m:
                    got = get_preview_bytes(m)
                    if got:
                        return self._send(got[0], got[1])
                return self._json({"error": "no preview"}, 404)

            if u.path == "/api/peers":
                data = gather_peers(cooked, slot, engine.get("cfg") or {}, _ident())
                data["manual_peers"] = (engine.get("cfg") or {}).get("manual_peers") or []
                return self._json(data)

            if u.path == "/api/lan":
                return self._json(_lan_status(engine))

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
                    if not self._local_ok():
                        return self._json({"error": "local only"}, 403)
                    new = str(payload.get("secret") or "").strip() or new_secret()
                    engine["secret"] = new
                    engine.setdefault("cfg", {})["session_secret"] = new
                    save_config(engine["cfg"])
                    return self._json({"ok": True, "secret": new})
                if u.path == "/api/network":
                    if not self._local_ok():
                        return self._json({"error": "local only"}, 403)
                    return self._json(_network_set(engine, payload))
                if u.path == "/api/update/mode":
                    if not self._local_ok():
                        return self._json({"error": "local only"}, 403)
                    return self._json(_update_set(engine, payload))
                if u.path == "/api/update/apply":
                    if not self._local_ok():
                        return self._json({"error": "local only"}, 403)
                    return self._json(_update_apply(engine))
                if u.path == "/api/peers/manual":
                    if not self._local_ok():
                        return self._json({"error": "local only"}, 403)
                    return self._json(_peers_manual_set(engine, payload))
                if u.path == "/api/peer/switch":
                    return self._json(peer_switch(cooked, slot, payload))
                if u.path == "/api/sync":
                    return self._json(sync_action(cooked, slot, engine.get("cfg") or {},
                                                  str(payload.get("mode") or "push"),
                                                  payload.get("peers") or []))
                if u.path == "/api/lan/start":
                    if not self._local_ok():
                        return self._json({"error": "local only"}, 403)
                    if engine.get("network_mode") == MODE_LAN:
                        return self._json({
                            "ok": False,
                            "error": "The discovery relay isn't needed in local-network mode - "
                                     "Rocket League's own LAN discovery works directly here."})
                    return self._json(_lan_start(engine, payload))
                if u.path == "/api/lan/stop":
                    if not self._local_ok():
                        return self._json({"error": "local only"}, 403)
                    return self._json(_lan_stop(engine))
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
               network: str | None = None,
               restore_on_exit: bool = False, window: bool = True) -> int:
    rpmap_log.install()           # tee stdout/stderr into the in-memory ring
    cfg = load_config()
    if secret:
        cfg["session_secret"] = secret
    if network in NETWORK_MODES:
        cfg["network_mode"] = network
    if not cfg.get("session_secret"):
        cfg["session_secret"] = new_secret()
    if not cfg.get("port"):
        cfg["port"] = port
    save_config(cfg)
    # Announce "what's new" once after an update: on a packaged build, remember
    # the version we were last run at and keep the previous one for this run so
    # the UI can show what changed. Source checkouts never self-update, so they
    # don't touch this.
    prev_version = None
    if update._update_target() is not None:
        prev_version = update.note_version_change(cfg, APP_VERSION)
    mode = network_mode(cfg)
    if not host:
        host = "0.0.0.0"          # all interfaces; requests are gated by source IP
    ident = local_identity(cfg)
    token = secrets.token_urlsafe(16)
    rpmap_log.set_secrets([token, cfg.get("session_secret") or ""])
    actual = _find_port(port) if host == "127.0.0.1" else port
    engine = {"cooked": cooked, "slot": slot, "token": token, "host": host,
              "secret": cfg["session_secret"], "cfg": cfg, "ident": ident,
              "dev": dev, "port": actual, "network_mode": mode,
              "prev_version": prev_version}
    try:
        httpd = _HttpServer((host, actual), make_handler(engine))
    except OSError as e:
        print(f"could not listen on {host}:{actual}: {e}", file=sys.stderr)
        print("hint: another rpmap may already be running (`rpmap stop`), "
              "or pass a different --port", file=sys.stderr)
        return 1
    write_runtime({"pid": os.getpid(), "port": actual, "host": host,
                   "token": token, "version": APP_VERSION,
                   "started": int(time.time())})
    url = f"http://{_ui_host(host)}:{actual}/?token={token}"
    print(f"rpmap UI  ->  {url}", flush=True)
    print(f"slot: {SLOTS[slot][0]}   cooked: {cooked}", flush=True)
    print(f"peer: {ident['name']} ({ident['peer_id']})", flush=True)
    ips = ident.get("ips") or []
    if mode == MODE_LAN:
        print(f"network: local network ({ips[0] if ips else 'no LAN address found'})",
              flush=True)
    else:
        print(f"network: Tailscale ({ips[0] if ips else 'not up - run `sudo tailscale up`'})",
              flush=True)
    print(f"session: {cfg['session_secret']}   <- share with peers / paste in the UI", flush=True)
    if host == "127.0.0.1":
        print("hint: for peer sync run:  rpmap serve --host <your-ip>", flush=True)
    else:
        print(f"note: listening on {host}:{actual}; peers reach this on your network", flush=True)
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

    lan = engine.get("lan")
    if lan:
        lan.stop()
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

    def _status() -> tuple[str, str, str]:
        st = slot_state(cooked, slot)
        arena = SLOT_INFO.get(slot, {}).get("arena", slot)
        if st["state"] == "stock":
            what = "stock map"
        elif st.get("map_name"):
            what = st["map_name"]
        else:
            what = {"custom": "custom map", "missing": "slot file missing",
                    "unknown": "unrecognised map"}.get(st["state"], st["state"])
        mode = engine.get("network_mode") or MODE_TAILSCALE
        where = "local network" if mode == MODE_LAN else "Tailscale"
        return arena, what, where

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
