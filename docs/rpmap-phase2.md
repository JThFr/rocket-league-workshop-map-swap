# Phase 2 — peer sync over Tailscale (design spec)

Status: **implemented (reference-only).** Phase 1 (slot swap + visual UI) and
Phase 2 M0–M3 live in `scripts/rpmap.py`: config file + identity + session secret
+ Bearer auth + source-IP/Host guards; `GET /api/capabilities`, `/api/session`,
`GET /api/peers` (live roster, in-sync by sha256), `POST /api/peer/switch`, and
`POST /api/sync` (push/pull) with UI sync buttons.

**Decision:** map *content transfer* was dropped in favour of **reference-only**
sync — each peer loads the map from its own copy (a Steam Workshop subscription,
or the same file). No bytes move and there is no cache. A peer missing the map
returns `not installed` with a Workshop link. Non-Workshop maps sync only if the
peer already has the byte-identical file (matched by sha256). See §Propagation
below for the original transfer design (not used).

## Goal

While a group is connected over Tailscale, each `rpmap.py` instance should be able to:

1. **See the other players** (peers on the tailnet).
2. **Show what map each peer has loaded** in their slot.
3. **Confirm everyone is in sync** (same map content, by hash — not just the same name).
4. **Propagate a switch** (optional): when someone picks a map, the others fetch it (if they
   don't have it) and swap to the same map, so a session can move maps without coordinating
   by hand.

## Non-goals (for now)

- No accounts or central server. Tailnet-only, peer-to-peer.
- No swapping during an **online** match — swaps happen at the main menu only.
- Not replacing Rocket Plugin for hosting. Phase 2 is about the map slot + sync.

## Building blocks already present in `rpmap.py`

- `list_maps(cooked)` — workshop / `mods/` / `~/maps`, with ids and previews.
- `slot_state(cooked, slot)` — size + **sha256** match identifies the loaded map
  (`state`: `stock | custom | unknown | missing`, plus `map_name`/`map_id`).
- `use_map` / `restore_slot` — atomic swap with a one-time stock backup.
- HTTP server + token auth + `Host` allow-list; routes `/api/state`, `/api/preview`,
  `/api/use`, `/api/restore`.
- `run_server(host, port)` already binds to a non-loopback host (needed here).
- `/api/peers` — **stub** that returns `tailscale status --json` peers, `"enabled": false`.
- `tailscale_peers()` — best-effort peer list (needs `tailscaled` running; may need root).

## Proposed architecture

1. **Identity.** A stable `peer_id` per machine = the Tailscale hostname
   (`Self.HostName`), plus a display name. `tailscale status --json` gives
   `Self{}` and `Peer{}` (HostName, DNSName, TailscaleIPs, Online, OS).
   Fall back to a manual peer list in a config file if the daemon/socket isn't reachable.

2. **One server per machine, fixed port.** Default `8787`, bound to the tailnet
   (`--host 10x.x.x.x` or `0.0.0.0`). **Do not use 7777** — Rocket Plugin's file server owns it.

3. **Discovery.** `tailscale status --json` to enumerate peers; probe each peer's
   `/api/capabilities` (short timeout) to see who is running `rpmap` and on which port.
   Cache results; show online/offline.

4. **Handshake / auth.** A **shared session secret** the host creates and shares out of band
   (or via the UI). Every API call carries it. Optionally also rely on Tailscale ACLs to
   limit who can reach the port. Reject unknown `Host` headers (already done for loopback).

5. **Status polling.** `GET /api/state` on each peer →
   `{peer, slot:{state,map_name,map_id,size,sha256}}`. Compare **sha256** across peers.

6. **Propagation (implemented, reference-only).**
   - `POST /api/peer/switch` with `{workshop_id?, name?, sha256?}` → the peer looks up the map
     **locally** (Workshop id → hash → name), verifies the hash, then `use_map`. If it lacks the
     map it returns `{error:"not installed", url:<workshop>}`; a hash mismatch returns
     `{error:"version mismatch"}` and does **not** swap.
   - `POST /api/sync {mode:"push"|"pull", peers:[{ip,port,name}]}` drives it from the UI: push
     sends this machine's map reference to peers; pull loads a peer's map here.
   - **No byte transfer** (the original `GET /api/map` + cache design was dropped — see the
     status note). Never swap while an online match is active: enforced best-effort by requiring
     an explicit action plus a UI warning (no reliable match-state signal is available).

7. **UI (implemented).** The "Players" panel is live: roster rows (name · IP · loaded map ·
   size · hash · in-sync badge) with per-peer **Switch me** (pull) and **Set theirs** (push)
   buttons, plus **Load mine on everyone** (push all). A session-code box lets peers join.

## API sketch

_As originally designed (transfer in **italics** is not implemented):_

```
GET  /api/capabilities        -> {product, version, peer_id, name, port, slot:{..., sha256, workshop_id}}
GET  /api/state               -> {peer, slot:{..., sha256, workshop_id}}
GET  /api/peers               -> {self:{...}, peers:[{peer_id, name, ip, online, rpmap?, slot?, sha256?, in_sync?}]}
GET  /api/session             -> {peer_id, name, port, secret}
POST /api/peer/switch         -> {workshop_id?, name?, sha256?}   (resolve locally, verify, swap)
POST /api/sync                -> {mode:"push"|"pull", peers:[{ip,port,name}]}
POST /api/session             -> {secret}  (set/rotate the shared secret)
_GET  /api/map?sha256=<sha256> -> raw bytes (NOT implemented — reference-only)_
```

All `/api/*` calls require the shared secret (query param or header) and a valid `Host`.

## "In sync" definition

`in sync` = every peer reports the **same slot sha256** as the session's chosen map.
Display per peer: `same` / `different` / `unknown` (peer unreachable or hash missing).

## Security

- Bind to the tailnet (or `0.0.0.0` reachable only via `tailscale0`); never expose publicly.
- Shared secret on every call; unknown `Host` rejected (DNS-rebinding guard already present).
- Only accept a transferred map whose bytes hash to the announced `sha256`.
- Never auto-swap during an online match.
- Keep the existing "restore before online play" rule visible in the UI.

## UI changes

- `Players` panel becomes live: auto-refresh alongside `/api/state`.
- Per-peer row: name, online dot, loaded map, sync badge.
- Actions: "Sync me to them", "Sync everyone to <map>".
- Show a spinner / progress for map transfers (files can be 100 MB+).

## Test plan

1. Two machines on the tailnet, both: `python3 rpmap.py serve --host <tailscale-ip>`.
2. Each lists the other under Players.
3. Change the map on A → B shows **out of sync** (different sha256).
4. "Sync B to A" → B fetches (if needed) + swaps; both report the same sha256.
5. Test with the real **friend's Windows** box (`python rpmap.py`).
6. Edge cases: peer offline; peer lacks the file; hash mismatch; large map transfer;
   swap attempted mid-match (must be refused).

## Open decisions

- Identity source: Tailscale hostname vs a configured display name (do both?).
- Explicit "sync" vs automatic propagation — **recommend explicit first**.
- Fixed port vs advertised/discovered port.
- How the shared secret is created and shared (UI paste box? a file? Tailscale ACL only?).
- Should Phase 2 also drive Rocket Plugin (host the swapped arena) or stay swap-only?

## Reference

- Ports: Rocket Plugin file server = **TCP 7777**; `rpmap.py` UI = **8787**.
- `tailscale status --json` needs `tailscaled` running (`sudo systemctl start tailscaled`);
  the socket may require root depending on the install.
- Slot file (default): `CookedPCConsole/Labs_Utopia_P.upk` (arena **Utopia Retro**); backup
  `…upk.orig`. `--slot underpass` uses `Labs_Underpass_P.upk` (Rumble) instead.
- Swap rules: pick the slot's arena in Free Play; same file on every player; restore before
  online play (or run `serve --restore-on-exit`).
