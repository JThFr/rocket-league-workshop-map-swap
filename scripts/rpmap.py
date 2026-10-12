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
  python3 rpmap.py                      # visual interface (local web UI)
  python3 rpmap.py serve [--host H --port P] [--no-browser] [--secret CODE]
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

from rpmap_config import (APP_VERSION, PRODUCT, SLOTS, SLOT_INFO,
                          DEFAULT_SLOT, config_path, load_config, new_secret,
                          save_config)
from rpmap_maps import (detect_cooked, fetch_host_map, host_status, install_map,
                        list_maps, resolve_map, restore_slot, slot_state, use_map)
from rpmap_peers import gather_peers, local_identity
from rpmap_update import check_for_update, maybe_self_update
from rpmap_web import run_server


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
    p_srv.add_argument("--host", default="127.0.0.1")
    p_srv.add_argument("--port", type=int, default=8787)
    p_srv.add_argument("--no-browser", action="store_true")
    p_srv.add_argument("--secret", help="session code shared with peers")
    p_srv.add_argument("--dev", action="store_true",
                       help="also accept private-LAN peers (testing without Tailscale)")
    p_srv.add_argument("--restore-on-exit", dest="restore_on_exit_serve", action="store_true",
                       help="restore the stock map when the program exits cleanly")
    sub.add_parser("peers", help="show tailnet peers and their loaded maps")
    p_ss = sub.add_parser("session", help="show or rotate the shared session code")
    p_ss.add_argument("--new", action="store_true", help="generate a new code")
    p_jn = sub.add_parser("join", help="save a session code shared by a peer")
    p_jn.add_argument("code")
    p_nm = sub.add_parser("name", help="set the display name peers see")
    p_nm.add_argument("value")
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

    if args.cmd in (None, "serve") and not args.no_update_check:
        maybe_self_update(auto=True)

    cooked = detect_cooked(args.cooked)
    if cooked is None:
        print("Could not find Rocket League's CookedPCConsole.", file=sys.stderr)
        print("Pass --cooked /path or set RL_COOKED.", file=sys.stderr)
        return 1

    if args.cmd in (None, "serve"):
        return run_server(cooked, args.slot,
                          getattr(args, "host", "127.0.0.1"),
                          getattr(args, "port", 8787),
                          not getattr(args, "no_browser", False),
                          secret=getattr(args, "secret", None),
                          dev=getattr(args, "dev", False),
                          restore_on_exit=(getattr(args, "restore_on_exit_serve", False)
                                           or getattr(args, "restore_on_exit", False)))

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
                loaded = "unreachable"
            dot = "online" if p.get("online") else "offline"
            ver = "v" + (p.get("version") or "?")
            print(f"  [{dot:<7}] {str(p.get('name') or '?'):<22} "
                  f"{str(p.get('ip') or '?'):<16} {loaded:<28} {str(p.get('in_sync')):<9} {ver}")
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
