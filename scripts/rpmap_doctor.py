"""``rpmap doctor`` - a one-shot environment/health report.

Diagnoses the usual "why isn't this working?" causes in one command: is Rocket
League found, is the slot sane and backed up, is the UI running, is Tailscale
up, is a session code set, will self-update verify TLS, can the status window
show, and who on the tailnet is actually serving rpmap.

It never changes anything and it never raises: each probe is best-effort, so a
single failure still prints the rest of the report.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from rpmap_config import (APP_VERSION, MODE_LAN, PRODUCT, SLOT_INFO, config_path,
                          load_config, network_mode)
import rpmap_maps as maps
import rpmap_peers as peers
import rpmap_update as update
import rpmap_web as web
import rpmap_window as window

_MARK = {"ok": "ok", "warn": "!!", "info": "--", "fail": "XX"}


def _print(checks: list[tuple]) -> None:
    print(f"{PRODUCT} doctor")
    for status, text, *rest in checks:
        print(f"  [{_MARK.get(status, status)}] {text}")
        if rest and rest[0]:
            print(f"         -> {rest[0]}")


def _slot_summary(cooked: Path, slot: str) -> str:
    st = maps.slot_state(cooked, slot)
    arena = SLOT_INFO.get(slot, {}).get("arena", slot)
    what = st.get("map_name") or {"stock": "stock map", "custom": "custom map",
                                  "missing": "slot file missing"}.get(
                                      st.get("state"), st.get("state"))
    return f"{arena}: {what} [{st.get('state')}]"


def run(cooked_arg: str | None, slot: str) -> int:
    """Print the health report; return 1 if a hard check fails."""
    cfg = load_config()
    checks: list[tuple] = []

    frozen = bool(getattr(sys, "frozen", False))
    checks.append(("ok", f"{PRODUCT} {APP_VERSION} "
                         f"({'packaged' if frozen else 'from source'})"))
    checks.append(("ok", f"config: {config_path()}"))

    # --- Rocket League ---------------------------------------------------- #
    try:
        cooked = maps.detect_cooked(cooked_arg)
    except Exception as e:
        cooked = None
        checks.append(("warn", f"Rocket League: detection failed: {e}"))
    if cooked is None:
        checks.append(("fail", "Rocket League: CookedPCConsole not found",
                       "pass --cooked /path or set RL_COOKED"))
        _print(checks)
        return 1
    checks.append(("ok", f"Rocket League: {cooked}"))

    # --- slot ------------------------------------------------------------- #
    try:
        summary = _slot_summary(cooked, slot)
        checks.append(("ok", f"slot: {summary}"))
        st = maps.slot_state(cooked, slot)
        if st["state"] in ("custom", "unknown") and not st.get("backup_exists"):
            checks.append(("warn", "slot has no stock backup",
                           "swaps can't be reverted to stock - reinstall/verify "
                           "the arena file, then `rpmap use` will take a backup"))
    except Exception as e:
        checks.append(("warn", f"slot: could not read state: {e}"))

    # --- running UI ------------------------------------------------------- #
    try:
        live = web.running_instance()
        if live:
            checks.append(("ok", f"UI running (pid {live.get('pid')}) on port "
                                 f"{live.get('port')} - stop it with `rpmap stop`"))
        else:
            checks.append(("info", "UI not running"))
    except Exception as e:
        checks.append(("warn", f"UI: {e}"))

    # --- identity / connection mode -------------------------------------- #
    mode = network_mode(cfg)
    try:
        ident = peers.local_identity(cfg)
        if mode == MODE_LAN:
            ip = (ident.get("ips") or [None])[0]
            if ip:
                checks.append(("ok", f"network: local network ({ip})"))
            else:
                checks.append(("warn", "network: local network, no LAN address found",
                               "check this machine's network connection"))
        else:
            ts = peers.tailscale_self()
            if ts.get("ips"):
                checks.append(("ok", f"network: Tailscale ({ident['name']}) {ts['ips'][0]}"))
            else:
                checks.append(("warn", "network: Tailscale selected, no tailnet address",
                               "is tailscaled up and logged in? `sudo tailscale up`"))
    except Exception as e:
        ident = {}
        checks.append(("warn", f"network: {e}"))

    # --- session code ----------------------------------------------------- #
    if cfg.get("session_secret"):
        checks.append(("ok", "session code set"))
    else:
        checks.append(("warn", "session code not set",
                       "run `rpmap session --new` (or paste a peer's code)"))

    # --- self-update TLS -------------------------------------------------- #
    if os.name == "nt":
        checks.append(("ok", "TLS: Windows uses the OS trust store"))
    elif os.environ.get("SSL_CERT_FILE"):
        checks.append(("ok", f"TLS: CA bundle {os.environ['SSL_CERT_FILE']}"))
    else:
        checks.append(("warn", "TLS: no CA bundle found",
                       "self-update may fail certificate verification"))

    # --- status window ---------------------------------------------------- #
    try:
        checks.append(("ok", "status window: available") if window.available()
                      else ("info", "status window: unavailable (runs headless)"))
    except Exception as e:
        checks.append(("info", f"status window: unavailable ({e})"))

    # --- newer release ---------------------------------------------------- #
    try:
        info = update.check_for_update()
        if info:
            checks.append(("info", f"update: {info['tag']} available -> {info['url']}"))
        else:
            checks.append(("ok", "update: no newer release found"))
    except Exception as e:
        checks.append(("info", f"update: check skipped ({e})"))

    # --- peers ------------------------------------------------------------ #
    try:
        data = peers.gather_peers(cooked, slot, cfg, ident or {})
        roster = data.get("peers") or []
        reachable = sum(1 for p in roster if p.get("rpmap"))
        where = "on your network" if mode == MODE_LAN else "on the tailnet"
        if not roster:
            fix = ("add peers by IP in the UI" if mode == MODE_LAN
                   else "bring a peer online on the tailnet")
            checks.append(("info", f"peers: none {where}", fix))
        else:
            fix = ("" if reachable else "start rpmap there (same session code)"
                   if mode == MODE_LAN else
                   "start rpmap there with `serve --host <tailscale-ip>`")
            checks.append(("ok" if reachable else "warn",
                           f"peers: {len(roster)} {where}, {reachable} serving rpmap",
                           fix))
    except Exception as e:
        checks.append(("warn", f"peers: {e}"))

    _print(checks)
    return 1 if any(c[0] == "fail" for c in checks) else 0
