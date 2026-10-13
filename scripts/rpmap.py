#!/usr/bin/env python3
"""rpmap - Rocket League custom-map helper (Linux + Windows). Standard library only.

Two independent jobs:

  1. "slot swap" (no mods, no restart)
     Rocket League loads a stock arena package whenever you pick that arena.
     Overwrite that one file and the game loads your custom map - no BakkesMod,
     no Rocket Plugin, no restart. The default slot is **Utopia Retro**
     (`Labs_Utopia_P.upk`), which isn't used in online playlists; **Underpass**
     (`Labs_Underpass_P.upk`, used in Rumble) is also available via `--slot`.
     The tool backs up the stock file and swaps maps in and out.

  2. Rocket Plugin staging
     Copy maps into CookedPCConsole/{mods,rocketplugin} so BakkesMod's Rocket
     Plugin sees them without its "Copy Map and Restart" step.

Usage:
  python3 rpmap.py                      # status window + visual interface (web UI)
  python3 rpmap.py serve [--host H --port P] [--network tailscale|lan] [--secret CODE] [--no-window]
  python3 rpmap.py list                 # candidate maps (workshop/mods/~/maps)
  python3 rpmap.py current [--slot s]   # what is loaded in the slot
  python3 rpmap.py use <name|path>      # swap a map into the slot (backs up first)
  python3 rpmap.py restore              # restore the stock map
  python3 rpmap.py install <file>       # stage a map for Rocket Plugin
  python3 rpmap.py status <host> [port] # ask a Rocket Plugin host for its map
  python3 rpmap.py fetch  <host> [port] # download the host's map + stage it
  python3 rpmap.py peers                # show tailnet peers and their loaded maps
  python3 rpmap.py session [--new]      # show or rotate the shared session code
  python3 rpmap.py join <code>          # save a session code shared by a peer
  python3 rpmap.py name <display>       # set the name peers see
  python3 rpmap.py network [tailscale|lan] # show or set the peer connection mode
  python3 rpmap.py stop                 # stop a running UI (or use its Quit button)
  python3 rpmap.py doctor               # diagnose paths, connection mode, peers, TLS
  python3 rpmap.py lan-host [--peer IP]... # relay native LAN discovery (you host)
  python3 rpmap.py lan-join --host IP   # receive relayed LAN discovery (you join)
  python3 rpmap.py version              # print the version
  python3 rpmap.py check-update         # check GitHub for a newer release

Env:
  RL_COOKED   override the CookedPCConsole path
  RL_MAPS     extra folder of .udk/.upk maps (default ~/maps)

Phase 2 (in progress): peers over Tailscale. Run `serve --host <tailnet-ip>`,
share the printed session code, and see who has which map loaded (compared by
sha256). Pushing map switches to peers is not wired up yet.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import rpmap_doctor
import rpmap_lan
from rpmap_config import (APP_VERSION, NETWORK_MODES, PRODUCT, SLOTS, SLOT_INFO,
                          DEFAULT_SLOT, auto_update_setting, config_path,
                          load_config, network_mode, new_secret, save_config)
from rpmap_maps import (detect_cooked, fetch_host_map, host_status, install_map,
                        list_maps, resolve_map, restore_slot, slot_state, use_map)
from rpmap_peers import gather_peers, local_identity
from rpmap_update import check_for_update, maybe_self_update
from rpmap_web import (open_browser, running_instance, run_server, stop_instance,
                       ui_url)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _fmt(n: int) -> str:
    u = ["B", "KB", "MB", "GB"]
    f, i = float(n), 0
    while f >= 1024 and i < len(u) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {u[i]}"


def _slot_label(s: dict) -> str:
    if s["state"] == "stock":
        return "Stock map"
    if s["map_name"]:
        return s["map_name"]
    return {"custom": "(custom map)", "unknown": "(not stock, not in map list)",
            "missing": "(missing)"}.get(s["state"], s["state"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rpmap", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cooked", help="path to TAGame/CookedPCConsole")
    ap.add_argument("--slot", default=DEFAULT_SLOT, choices=sorted(SLOTS))
    ap.add_argument("--no-update-check", action="store_true",
                    help="skip the startup check for a newer GitHub release")
    ap.add_argument("--no-window", dest="no_window", action="store_true",
                    help="don't show the desktop status window")
    ap.add_argument("--restore-on-exit", dest="restore_on_exit", action="store_true",
                    help="restore the stock map when the program exits cleanly")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list", help="list candidate maps")
    sub.add_parser("current", help="show what is loaded in the slot")
    p_use = sub.add_parser("use", help="swap a map into the slot")
    p_use.add_argument("map")
    sub.add_parser("restore", help="restore the stock map")
    p_ins = sub.add_parser("install", help="stage a map for Rocket Plugin")
    p_ins.add_argument("map")
    p_st = sub.add_parser("status", help="ask a Rocket Plugin host for its map")
    p_st.add_argument("host")
    p_st.add_argument("port", nargs="?", type=int, default=7777)
    p_fe = sub.add_parser("fetch", help="download the host's map + stage it")
    p_fe.add_argument("host")
    p_fe.add_argument("port", nargs="?", type=int, default=7777)
    p_srv = sub.add_parser("serve", help="run the visual interface")
    p_srv.add_argument("--host", default=None,
                       help="address to bind (default: all interfaces; requests are gated)")
    p_srv.add_argument("--port", type=int, default=8787)
    p_srv.add_argument("--no-browser", action="store_true")
    p_srv.add_argument("--secret", help="session code shared with peers")
    p_srv.add_argument("--network", choices=sorted(NETWORK_MODES),
                       help="peer connection mode: tailscale (default) or lan")
    p_srv.add_argument("--dev", action="store_true",
                       help="also accept private-LAN peers (testing without Tailscale)")
    p_srv.add_argument("--restore-on-exit", dest="restore_on_exit_serve", action="store_true",
                       help="restore the stock map when the program exits cleanly")
    p_srv.add_argument("--no-window", dest="no_window_serve", action="store_true",
                       help="don't show the desktop status window")
    sub.add_parser("peers", help="show tailnet peers and their loaded maps")
    p_ss = sub.add_parser("session", help="show or rotate the shared session code")
    p_ss.add_argument("--new", action="store_true", help="generate a new code")
    p_jn = sub.add_parser("join", help="save a session code shared by a peer")
    p_jn.add_argument("code")
    p_nm = sub.add_parser("name", help="set the display name peers see")
    p_nm.add_argument("value")
    p_net = sub.add_parser("network", help="show or set the connection mode")
    p_net.add_argument("value", nargs="?", choices=sorted(NETWORK_MODES),
                       help="tailscale (default) or lan; omit to print the current mode")
    sub.add_parser("stop", help="stop a running rpmap UI")
    sub.add_parser("doctor", help="diagnose the environment (paths, peers, Tailscale, TLS)")

    def _add_lan_args(p):
        p.add_argument("--token", help="shared secret (default: the shared session code)")
        p.add_argument("--relay-port", type=int, default=rpmap_lan.DEFAULT_RELAY_PORT,
                       help="relay UDP port (default %(default)s)")
        p.add_argument("--bind", help="relay socket address (default: your tailnet IP)")
        p.add_argument("--inject", choices=("broadcast", "loopback"), default="broadcast",
                       help="how to replay a message into RL (default %(default)s)")
        p.add_argument("--lan-port", type=int, default=rpmap_lan.LAN_PORT,
                       help="RL's LAN discovery port (default %(default)s)")
        p.add_argument("--dev", action="store_true",
                       help="also accept private-LAN relay senders (testing)")

    p_lh = sub.add_parser("lan-host", help="relay native LAN discovery to tailnet peers")
    p_lh.add_argument("--peer", action="append", default=[],
                      help="tailnet IP of a joiner (repeatable; default: all online peers)")
    p_lh.add_argument("--advertise",
                      help="IP written into ServerID (default: your tailscale IP)")
    _add_lan_args(p_lh)

    p_lj = sub.add_parser("lan-join", help="receive relayed native LAN discovery")
    p_lj.add_argument("--host", required=True, help="tailnet IP of the host")
    _add_lan_args(p_lj)

    sub.add_parser("version", help="print the version")
    sub.add_parser("check-update", help="check GitHub for a newer release")

    args = ap.parse_args(argv)

    if args.cmd == "session":
        cfg = load_config()
        if args.new or not cfg.get("session_secret"):
            cfg["session_secret"] = new_secret()
            save_config(cfg)
            print("new session code:", cfg["session_secret"])
        else:
            print(cfg["session_secret"])
        return 0

    if args.cmd == "join":
        cfg = load_config()
        cfg["session_secret"] = args.code.strip()
        save_config(cfg)
        print("joined session; code saved to", config_path())
        return 0

    if args.cmd == "name":
        cfg = load_config()
        cfg["display_name"] = args.value.strip() or None
        save_config(cfg)
        print("display name:", cfg.get("display_name") or "(unset)")
        return 0

    if args.cmd == "network":
        cfg = load_config()
        if args.value:
            cfg["network_mode"] = args.value
            save_config(cfg)
        print("network mode:", network_mode(cfg))
        return 0

    if args.cmd == "stop":
        if stop_instance():
            print("stopping the running rpmap UI")
            return 0
        print("rpmap is not running")
        return 0

    if args.cmd == "doctor":
        return rpmap_doctor.run(args.cooked, args.slot)

    if args.cmd in ("lan-host", "lan-join"):
        cfg = load_config()
        token = (args.token or "").strip() or cfg.get("session_secret") or ""
        if not token:
            print("no shared secret: pass --token or set the session code "
                  "(`rpmap session --new`)", file=sys.stderr)
            return 1
        if args.cmd == "lan-host":
            return rpmap_lan.run_host(
                peers=rpmap_lan.resolve_peers(cfg, args.peer), token=token,
                advertise=args.advertise, relay_port=args.relay_port,
                bind=args.bind, inject_mode=args.inject, lan_port=args.lan_port,
                dev=args.dev)
        return rpmap_lan.run_join(
            host=args.host, token=token, relay_port=args.relay_port,
            bind=args.bind, inject_mode=args.inject, lan_port=args.lan_port,
            dev=args.dev)

    if args.cmd == "version":
        print(f"{PRODUCT} {APP_VERSION}")
        return 0

    if args.cmd == "check-update":
        info = check_for_update()
        if info:
            print(f"{PRODUCT} {APP_VERSION}: {info['tag']} available  ->  {info['url']}")
        else:
            print(f"{PRODUCT} {APP_VERSION}: up to date (no newer release found)")
        return 0

    if args.cmd in (None, "serve"):
        live = running_instance()
        if live:
            url = ui_url(live)
            print(f"rpmap is already running (pid {live['pid']}) -> {url}")
            if not getattr(args, "no_browser", False):
                open_browser(url)
            return 0

    if args.cmd in (None, "serve") and not args.no_update_check:
        # Opt-in only: never contact GitHub (let alone install) until the user
        # has explicitly enabled auto-updates in the first-run popup / UI.
        if auto_update_setting(load_config()) is True:
            maybe_self_update(auto=True)

    cooked = detect_cooked(args.cooked)
    if cooked is None:
        print("Could not find Rocket League's CookedPCConsole.", file=sys.stderr)
        print("Pass --cooked /path or set RL_COOKED.", file=sys.stderr)
        return 1

    if args.cmd in (None, "serve"):
        return run_server(cooked, args.slot,
                          getattr(args, "host", None),
                          getattr(args, "port", 8787),
                          not getattr(args, "no_browser", False),
                          secret=getattr(args, "secret", None),
                          dev=getattr(args, "dev", False),
                          network=getattr(args, "network", None),
                          restore_on_exit=(getattr(args, "restore_on_exit_serve", False)
                                           or getattr(args, "restore_on_exit", False)),
                          window=(not getattr(args, "no_window_serve", False)
                                  and not getattr(args, "no_window", False)))

    if args.cmd == "list":
        for m in list_maps(cooked):
            print(f"  {m['name']:<40} {_fmt(m['size']):>10}  [{m['source']}]")
        return 0

    if args.cmd == "peers":
        cfg = load_config()
        ident = local_identity(cfg)
        data = gather_peers(cooked, args.slot, cfg, ident)
        me = data["self"]
        print(f"rpmap v{APP_VERSION}")
        mine = _slot_label({"state": me["slot"]["state"], "map_name": me["slot"]["map_name"]})
        print(f"  [you] {me['name']:<22} {str(me.get('ip') or '?'):<16} {mine}")
        for p in data["peers"]:
            slot = p.get("slot") or {}
            loaded = _slot_label({"state": slot.get("state"), "map_name": slot.get("map_name")})
            if not p.get("rpmap"):
                loaded = {"offline": "offline",
                          "not-rpmap": "online, not rpmap",
                          "no-inventory": "rpmap (no map list)"}.get(
                              p.get("reach"), "unreachable")
            dot = "online" if p.get("online") else "offline"
            ver = "v" + (p.get("version") or "?")
            print(f"  [{dot:<7}] {str(p.get('name') or '?'):<22} "
                  f"{str(p.get('ip') or '?'):<16} {loaded:<28} {str(p.get('in_sync')):<9} {ver}")
        unreachable = sum(1 for p in data["peers"] if not p.get("rpmap"))
        if unreachable:
            print(f"  ({unreachable} peer(s) not serving rpmap - start it there with "
                  f"`serve --host <tailscale-ip>`)")
        if not data["peers"]:
            print("  (no peers found - start tailscaled, or set manual_peers in config)")
        return 0

    if args.cmd == "current":
        s = slot_state(cooked, args.slot)
        print(f"map:    {_slot_label(s)}   [{s['state']}]")
        print(f"slot:   {s['slot_file']}")
        print(f"size:   {_fmt(s['size'])}")
        print(f"backup: {s['backup_path'] if s['backup_exists'] else 'none'}")
        return 0

    if args.cmd == "use":
        m = resolve_map(cooked, args.map)
        if not m:
            print(f"No map matched '{args.map}'. Try: rpmap list", file=sys.stderr)
            return 1
        use_map(cooked, args.slot, Path(m["path"]))
        info = SLOT_INFO.get(args.slot, {})
        arena = info.get("arena", args.slot)
        note = f" ({info['note']})" if info.get("note") else ""
        print(f"Loaded '{m['name']}' into the {arena} slot.")
        print(f'In game: Free Play -> arena "{arena}"{note}.')
        return 0

    if args.cmd == "restore":
        restore_slot(cooked, args.slot)
        print("Stock map restored.")
        return 0

    if args.cmd == "install":
        for p in install_map(cooked, Path(args.map)):
            print("installed:", p)
        print("(Rocket Plugin) restart RL so it loads the map before you host/join.")
        return 0

    if args.cmd == "status":
        try:
            print(json.dumps(host_status(args.host, args.port), indent=2))
        except Exception as e:
            print(f"Could not query {args.host}:{args.port}: {e}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "fetch":
        try:
            name, staged = fetch_host_map(cooked, args.host, args.port)
        except Exception as e:
            print(f"fetch failed: {e}", file=sys.stderr)
            return 1
        print(f"fetched '{name}':")
        for p in staged:
            print("  ", p)
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
