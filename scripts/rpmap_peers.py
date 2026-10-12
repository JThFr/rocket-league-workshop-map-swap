"""Tailnet peer discovery, probing and map sync for rpmap."""

from __future__ import annotations

import concurrent.futures
import json
import shutil
import socket
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from rpmap_config import APP_VERSION, DEFAULT_PORT, PRODUCT
from rpmap_maps import (_slot_paths, file_hash, find_map_by, map_inventory,
                        slot_state, use_map, workshop_url)


# --------------------------------------------------------------------------- #
# Peers (Phase 2): identity, discovery, probing
# --------------------------------------------------------------------------- #

def _tailscale_status() -> dict:
    exe = shutil.which("tailscale")
    if not exe:
        return {}
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True,
                             text=True, timeout=5).stdout
        return json.loads(out) if out and out.strip() else {}
    except Exception:
        return {}


def tailscale_self() -> dict:
    node = _tailscale_status().get("Self") or {}
    ips = [ip for ip in (node.get("TailscaleIPs") or []) if ":" not in ip]
    return {
        "peer_id": node.get("HostName") or socket.gethostname(),
        "dns": (node.get("DNSName") or "").rstrip("."),
        "ips": ips,
    }


def local_identity(cfg: dict) -> dict:
    node = tailscale_self()
    peer_id = node.get("peer_id") or socket.gethostname()
    return {
        "peer_id": peer_id,
        "name": cfg.get("display_name") or peer_id,
        "dns": node.get("dns") or "",
        "ips": node.get("ips") or [],
    }


def tailscale_peers(cfg: dict | None = None) -> list[dict]:
    """Tailnet peers (`tailscale status --json`), merged with any manual peers."""
    peers: list[dict] = []
    seen: set[str] = set()
    for _id, p in (_tailscale_status().get("Peer") or {}).items():
        ips = [ip for ip in (p.get("TailscaleIPs") or []) if ":" not in ip]
        name = p.get("HostName")
        seen.add((name or "").lower())
        peers.append({
            "peer_id": name,
            "name": name,
            "dns": (p.get("DNSName") or "").rstrip("."),
            "ip": (ips or [None])[0],
            "port": None,
            "online": bool(p.get("Online")),
            "os": p.get("OS"),
        })
    for m in (cfg or {}).get("manual_peers") or []:
        key = (m.get("name") or m.get("ip") or "").lower()
        if key in seen:
            continue
        seen.add(key)
        peers.append({"peer_id": m.get("name"), "name": m.get("name"), "dns": "",
                      "ip": m.get("ip"), "port": m.get("port"),
                      "online": True, "os": m.get("os")})
    peers.sort(key=lambda x: (x["name"] or ""))
    return peers



def _http_get_json_auth(url: str, secret: str, timeout: float = 2.0) -> dict:
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def probe_peer(ip: str | None, port: int, secret: str, timeout: float = 2.0) -> dict | None:
    """Return {'caps', 'state', 'maps'} if the peer speaks rpmap, else None.

    ``maps`` is the peer's inventory (or None if it predates ``/api/maps``).
    """
    if not ip:
        return None
    base = f"http://{ip}:{port}"
    try:
        caps = _http_get_json_auth(f"{base}/api/capabilities", secret, timeout)
        if caps.get("product") != PRODUCT:
            return None
        state = _http_get_json_auth(f"{base}/api/state", secret, timeout)
    except Exception:
        return None
    try:
        maps = _http_get_json_auth(f"{base}/api/maps", secret, timeout).get("maps")
    except Exception:
        maps = None                       # older peer: no inventory endpoint
    return {"caps": caps, "state": state, "maps": maps}


def common_map_keys(key_sets: list[set[str] | None]) -> list[str] | None:
    """Map keys present in every given set (peers we could actually query).

    Returns None when there is nothing to intersect, which the UI treats as
    "filter unavailable". ``gather_peers`` always passes this machine's own
    inventory too, so in practice the result is non-None whenever we have maps.
    """
    sets = [set(s) for s in key_sets if s is not None]
    if not sets:
        return None
    return sorted(set.intersection(*sets))


def _peer_row(p: dict, default_port: int, got: dict | None, local_hash: str) -> dict:
    pport = int(p.get("port") or default_port)
    row = {
        "peer_id": p.get("peer_id"), "name": p.get("name"), "ip": p.get("ip"),
        "online": bool(p.get("online")), "rpmap": False, "port": pport,
        "slot": None, "sha256": None, "in_sync": "unknown", "version": None,
        "map_count": None,
        # Why we couldn't reach it (kept meaningful for reachable rows too).
        "reach": "offline" if not p.get("online") else "not-rpmap",
    }
    if not got:
        return row
    st = (got.get("state") or {}).get("slot") or {}
    sha = st.get("sha256") or ""
    row.update({
        "rpmap": True, "online": True,
        "name": (got.get("caps") or {}).get("name") or row["name"],
        "version": (got.get("caps") or {}).get("version"),
        "slot": {"state": st.get("state"), "map_name": st.get("map_name"),
                 "size": st.get("size"), "workshop_id": st.get("workshop_id")},
        "sha256": sha or None,
    })
    if local_hash and sha:
        row["in_sync"] = "same" if sha == local_hash else "different"
    return row


def gather_peers(cooked: Path, slot: str, cfg: dict, ident: dict) -> dict:
    """Probe every peer in parallel and build the roster for this machine."""
    secret = cfg.get("session_secret") or ""
    port = int(cfg.get("port") or DEFAULT_PORT)
    local = slot_state(cooked, slot)
    local_hash = local.get("sha256") or ""
    peers = tailscale_peers(cfg)

    roster: list[dict] = []
    if peers:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(peers))) as ex:
            futs = {}
            for p in peers:
                pport = int(p.get("port") or port)
                futs[ex.submit(probe_peer, p.get("ip"), pport, secret)] = p
            for fut in concurrent.futures.as_completed(futs):
                try:
                    got = fut.result()
                except Exception:
                    got = None
                row = _peer_row(futs[fut], port, got, local_hash)
                row["_keys"] = ({e["key"] for e in got["maps"] if e.get("key")}
                                if got and got.get("maps") is not None else None)
                roster.append(row)

    # "Everyone" includes *this* machine, so the filter is meaningful (and the
    # UI toggle usable) even with no peers. The grid already intersects with our
    # own maps, so adding ourselves is a no-op when peers are present - but it
    # makes "everyone has" vacuously true when we're alone instead of undefined.
    self_keys = {e["key"] for e in map_inventory(cooked) if e.get("key")}
    contributors = [r["_keys"] for r in roster if r.get("rpmap") and r["_keys"] is not None]
    common = common_map_keys([self_keys, *contributors])
    for r in roster:
        r["map_count"] = len(r["_keys"]) if r["_keys"] is not None else None
        if r["rpmap"]:
            # reachable, but an old build may not expose /api/maps
            r["reach"] = "ok" if r["_keys"] is not None else "no-inventory"
        r.pop("_keys", None)
    roster.sort(key=lambda r: (r.get("name") or ""))

    return {
        "self": {
            "peer_id": ident.get("peer_id"), "name": ident.get("name"),
            "ip": (ident.get("ips") or [None])[0],
            "version": APP_VERSION,
            "slot": {"state": local.get("state"), "map_name": local.get("map_name"),
                     "size": local.get("size"), "sha256": local_hash or None,
                     "workshop_id": local.get("workshop_id")},
            "sha256": local_hash or None,
        },
        "peers": roster,
        "common": common,
    }


def _http_post_json_auth(url: str, secret: str, payload: dict, timeout: float = 10.0) -> dict:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        body = e.read()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    try:
        return json.loads(body)
    except Exception:
        return {"ok": False, "error": "bad response"}


def peer_switch(cooked: Path, slot: str, payload: dict) -> dict:
    """Server side of a push: apply a map reference sent by another peer."""
    wid, nm = payload.get("workshop_id"), payload.get("name")
    want = (payload.get("sha256") or "").lower() or None
    mp = find_map_by(cooked, workshop_id=wid, name=nm, sha256=want)
    if not mp:
        return {"ok": False, "error": "not installed", "workshop_id": wid,
                "name": nm, "url": workshop_url(wid)}
    got = file_hash(Path(mp["path"]))
    if want and got != want:
        return {"ok": False, "error": "version mismatch", "name": mp["name"],
                "expected": want, "have": got}
    res = use_map(cooked, slot, Path(mp["path"]))
    slot_file, _, _ = _slot_paths(cooked, slot)
    res["sha256"] = file_hash(slot_file)
    res["map_name"] = mp["name"]
    res["workshop_id"] = mp.get("workshop_id")
    return res


def sync_action(cooked: Path, slot: str, cfg: dict, mode: str, targets: list[dict]) -> dict:
    """Push this machine's map to peers, or pull a peer's map here.

    Reference-only: peers must already have the map (Workshop subscription or the
    same file). targets: [{ip, port, name}].
    """
    secret = cfg.get("session_secret") or ""
    results: list[dict] = []
    for t in targets:
        ip = (t.get("ip") or "").strip()
        port = int(t.get("port") or DEFAULT_PORT)
        who = t.get("name") or ip or "?"
        if not ip:
            results.append({"peer": who, "ok": False, "error": "no address"})
            continue
        try:
            if mode == "push":
                st = slot_state(cooked, slot)
                ref = {"workshop_id": st.get("workshop_id"), "name": st.get("map_name"),
                       "sha256": st.get("sha256")}
                r = _http_post_json_auth(f"http://{ip}:{port}/api/peer/switch", secret, ref)
            else:  # pull
                caps = _http_get_json_auth(f"http://{ip}:{port}/api/capabilities", secret, 3.0)
                ps = caps.get("slot") or {}
                if ps.get("state") == "stock" or not ps.get("sha256"):
                    results.append({"peer": who, "ok": False,
                                    "error": "peer is on the stock map"})
                    continue
                mp = find_map_by(cooked, workshop_id=ps.get("workshop_id"),
                                 name=ps.get("map_name"), sha256=ps.get("sha256"))
                if not mp:
                    results.append({"peer": who, "ok": False, "error": "not installed",
                                    "workshop_id": ps.get("workshop_id"),
                                    "url": workshop_url(ps.get("workshop_id"))})
                    continue
                if file_hash(Path(mp["path"])) != ps.get("sha256"):
                    results.append({"peer": who, "ok": False, "error": "version mismatch",
                                    "name": mp["name"]})
                    continue
                use_map(cooked, slot, Path(mp["path"]))
                r = {"ok": True, "map_name": mp["name"], "workshop_id": mp.get("workshop_id")}
            r.setdefault("peer", who)
            results.append(r)
        except Exception as e:
            results.append({"peer": who, "ok": False, "error": str(e)})
    return {"ok": bool(results) and all(r.get("ok") for r in results), "results": results}
