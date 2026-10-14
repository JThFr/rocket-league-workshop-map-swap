"""Tailnet peer discovery, probing and map sync for rpmap."""

from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from rpmap_config import (APP_VERSION, DEFAULT_PORT, MODE_LAN, PRODUCT,
                          network_mode)
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


def tailscale_installed() -> bool:
    """True when the ``tailscale`` CLI is on PATH (cheap; no subprocess)."""
    return shutil.which("tailscale") is not None


def local_ipv4s() -> list[str]:
    """This machine's own IPv4 addresses, primary route first (best effort).

    Used in local-network mode for identity and for the UI's Host allow-list.
    The first entry comes from the UDP-connect trick (no packet is sent - the
    kernel just picks the route); hostname resolution adds any further
    addresses. Pure standard library, never raises.
    """
    ips: list[str] = []

    def _add(ip: str) -> None:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return
        if addr.version == 4 and not addr.is_loopback and not addr.is_link_local:
            if str(addr) not in ips:
                ips.append(str(addr))

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            _add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add(info[4][0])
    except OSError:
        pass
    return ips


def local_identity(cfg: dict) -> dict:
    """Who this machine is, in the current network mode.

    Tailscale mode uses the tailnet hostname/IP; local-network mode uses the
    machine name and its LAN address, and never shells out to ``tailscale``.
    """
    if network_mode(cfg) == MODE_LAN:
        peer_id = socket.gethostname()
        return {
            "peer_id": peer_id,
            "name": cfg.get("display_name") or peer_id,
            "dns": "",
            "ips": local_ipv4s(),
        }
    node = tailscale_self()
    peer_id = node.get("peer_id") or socket.gethostname()
    return {
        "peer_id": peer_id,
        "name": cfg.get("display_name") or peer_id,
        "dns": node.get("dns") or "",
        "ips": node.get("ips") or [],
    }


def _manual_peers(cfg: dict | None, seen: set[str]) -> list[dict]:
    """The configured manual peers, skipping any ``seen`` name already present."""
    out: list[dict] = []
    for m in (cfg or {}).get("manual_peers") or []:
        key = (m.get("name") or m.get("ip") or "").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"peer_id": m.get("name"), "name": m.get("name"), "dns": "",
                    "ip": m.get("ip"), "port": m.get("port"), "manual": True,
                    "online": True, "os": m.get("os")})
    return out


def tailscale_peers(cfg: dict | None = None) -> list[dict]:
    """Tailnet peers (`tailscale status --json`), merged with any manual peers."""
    peers: list[dict] = []
    for _id, p in (_tailscale_status().get("Peer") or {}).items():
        ips = [ip for ip in (p.get("TailscaleIPs") or []) if ":" not in ip]
        name = p.get("HostName")
        peers.append({
            "peer_id": name,
            "name": name,
            "dns": (p.get("DNSName") or "").rstrip("."),
            "ip": (ips or [None])[0],
            "port": None,
            "online": bool(p.get("Online")),
            "os": p.get("OS"),
        })
    peers.extend(_manual_peers(cfg, {(p["name"] or "").lower() for p in peers}))
    peers.sort(key=lambda x: (x["name"] or ""))
    return peers


def known_peers(cfg: dict | None = None) -> list[dict]:
    """The peer roster to use in the current network mode.

    Tailscale mode is the full tailnet + manual list (:func:`tailscale_peers`);
    local-network mode has no tailnet, so it is the manual peers only - which is
    also the security boundary for sync targets.
    """
    cfg = cfg or {}
    if network_mode(cfg) != MODE_LAN:
        return tailscale_peers(cfg)
    peers = _manual_peers(cfg, set())
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


# This machine's own average round-trip time to its reachable peers, refreshed
# by :func:`gather_peers` and shared with peers via ``/api/capabilities`` (so a
# peer can tell whose average is lowest and mark the recommended host).
_self_ping_avg: float | None = None


def local_ping_avg() -> float | None:
    """This machine's last average ping to its peers, in ms (None until measured)."""
    return _self_ping_avg


def ping_peer(ip: str | None, port: int, secret: str,
              samples: int = 3, timeout: float = 2.0) -> float | None:
    """Average round-trip time to a peer's lightweight ``/api/ping``, in ms.

    HTTP RTT is a portable stand-in for ICMP: it needs no privileges, works in
    Tailscale *and* local-network modes, and travels the same path the peer API
    (and, in practice, the game) uses. Returns None if the peer is unreachable.
    """
    if not ip:
        return None
    url = f"http://{ip}:{port}/api/ping"
    vals: list[float] = []
    for _ in range(samples):
        t0 = time.perf_counter()
        try:
            _http_get_json_auth(url, secret, timeout)
        except Exception:
            return None
        vals.append((time.perf_counter() - t0) * 1000.0)
    return round(sum(vals) / len(vals), 1) if vals else None


def _probe_and_ping(ip: str | None, port: int, secret: str) -> tuple[dict | None, float | None]:
    got = probe_peer(ip, port, secret)
    return got, (ping_peer(ip, port, secret) if got else None)


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


def _sync_state(local: dict, peer: dict) -> str:
    """Whether a peer's loaded slot matches ours: ``same`` / ``different`` / ``unknown``.

    Stock maps are compared by **slot**, never by raw ``sha256``: the stock
    ``Labs_*.upk`` bytes change with the Rocket League build and can differ by
    platform, so two players on the *same* stock arena can legitimately hash
    differently (and the comparison reported a false "out of sync"). A peer on a
    different slot is a different arena and cannot be byte-compared at all.
    Only custom maps - the same Workshop file on both machines - are compared by
    content hash.
    """
    if not peer:
        return "unknown"
    lslot, pslot = local.get("slot"), peer.get("slot")
    if lslot and pslot and lslot != pslot:
        return "different"           # different arena (utopia vs underpass)
    lstate, pstate = local.get("state"), peer.get("state")
    if lstate == "stock" and pstate == "stock":
        return "same"                # same stock arena; the bytes aren't comparable
    if "stock" in (lstate, pstate):
        return "different"           # one is stock, the other a swapped-in map
    lh, ph = local.get("sha256") or "", peer.get("sha256") or ""
    if lh and ph:
        return "same" if lh == ph else "different"
    return "unknown"


def _peer_row(p: dict, default_port: int, got: dict | None, local: dict) -> dict:
    pport = int(p.get("port") or default_port)
    row = {
        "peer_id": p.get("peer_id"), "name": p.get("name"), "ip": p.get("ip"),
        "online": bool(p.get("online")), "rpmap": False, "port": pport,
        "manual": bool(p.get("manual")),
        "slot": None, "sha256": None, "in_sync": "unknown", "version": None,
        "map_count": None, "ping_ms": None, "peer_avg_ms": None,
        "recommended": False,
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
                 "size": st.get("size"), "workshop_id": st.get("workshop_id"),
                 "slot": st.get("slot"), "arena": st.get("arena")},
        "sha256": sha or None,
        # The peer's *own* average ping, shared via /api/capabilities.
        "peer_avg_ms": (got.get("caps") or {}).get("ping_ms"),
    })
    row["in_sync"] = _sync_state(local, st)
    return row


def _recommended_host(self_key: str, self_avg: float | None,
                      rows: list[dict]) -> str | None:
    """The node key whose *shared* average ping is lowest, or None.

    Compares this machine's own average with each reachable peer's self-reported
    average (``peer_avg_ms``). Needs at least two candidates, so a lone machine
    is never "recommended" - there is nothing to compare against.
    """
    averages: list[tuple[str, float]] = []
    if self_avg is not None:
        averages.append((self_key, self_avg))
    for r in rows:
        if r.get("rpmap") and r.get("peer_avg_ms") is not None:
            key = r.get("peer_id") or r.get("name") or r.get("ip") or "?"
            averages.append((key, float(r["peer_avg_ms"])))
    return min(averages, key=lambda kv: kv[1])[0] if len(averages) >= 2 else None


def gather_peers(cooked: Path, slot: str, cfg: dict, ident: dict) -> dict:
    """Probe every peer in parallel and build the roster for this machine.

    Also measures the round-trip time to each reachable peer, averages this
    machine's own pings (shared with peers so they can spot the lowest), and
    flags the node with the lowest average as the recommended host.
    """
    global _self_ping_avg
    secret = cfg.get("session_secret") or ""
    port = int(cfg.get("port") or DEFAULT_PORT)
    local = slot_state(cooked, slot)
    local_hash = local.get("sha256") or ""
    peers = known_peers(cfg)

    roster: list[dict] = []
    if peers:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(peers))) as ex:
            futs = {}
            for p in peers:
                pport = int(p.get("port") or port)
                futs[ex.submit(_probe_and_ping, p.get("ip"), pport, secret)] = p
            for fut in concurrent.futures.as_completed(futs):
                try:
                    got, ping = fut.result()
                except Exception:
                    got, ping = None, None
                row = _peer_row(futs[fut], port, got, local)
                row["ping_ms"] = ping
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

    # Our own average ping, and the recommended host: the node whose *shared*
    # average is lowest (needs at least two candidates to be a comparison).
    measured = [r["ping_ms"] for r in roster
                if r.get("rpmap") and r.get("ping_ms") is not None]
    _self_ping_avg = round(sum(measured) / len(measured), 1) if measured else None

    self_key = ident.get("peer_id") or "self"
    recommended = _recommended_host(self_key, _self_ping_avg, roster)
    for r in roster:
        r["recommended"] = bool(recommended) and \
            (r.get("peer_id") or r.get("name") or r.get("ip")) == recommended

    return {
        "self": {
            "peer_id": ident.get("peer_id"), "name": ident.get("name"),
            "ip": (ident.get("ips") or [None])[0],
            "version": APP_VERSION,
            "slot": {"state": local.get("state"), "map_name": local.get("map_name"),
                     "size": local.get("size"), "sha256": local_hash or None,
                     "workshop_id": local.get("workshop_id"),
                     "slot": local.get("slot"), "arena": local.get("arena")},
            "sha256": local_hash or None,
            "map_count": len(self_keys),
            "ping_ms": _self_ping_avg,
            "recommended": bool(recommended) and recommended == self_key,
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


def _roster_targets(cfg: dict, default_port: int) -> dict[str, int]:
    """ip -> the port we may contact that peer on, for known roster peers.

    Sync targets must be peers this machine actually knows (the tailnet roster
    from ``tailscale status`` in Tailscale mode, or the configured
    ``manual_peers`` in local-network mode). Without this check ``/api/sync``
    would connect to any ``ip:port`` a caller names - turning this machine into
    a request proxy and sending the shared session secret to a host it was never
    meant for.
    """
    allowed: dict[str, int] = {}
    for p in known_peers(cfg):
        ip = (p.get("ip") or "").strip()
        if not ip:
            continue
        allowed[ip] = int(p.get("port") or default_port)
    return allowed


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
    same file). targets: [{ip, port, name}] - each must be a known roster peer.
    """
    secret = cfg.get("session_secret") or ""
    default_port = int(cfg.get("port") or DEFAULT_PORT)
    allowed = _roster_targets(cfg, default_port)
    results: list[dict] = []
    for t in targets:
        ip = (t.get("ip") or "").strip()
        port = int(t.get("port") or default_port)
        who = t.get("name") or ip or "?"
        if not ip:
            results.append({"peer": who, "ok": False, "error": "no address"})
            continue
        if allowed.get(ip) != port:
            results.append({"peer": who, "ok": False, "error": "not a known peer"})
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
