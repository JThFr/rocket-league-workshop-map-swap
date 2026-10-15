# rocket-league-workshop-swap

**`rpmap`** — a helper to play **custom / workshop maps with friends in a Rocket League
multiplayer lobby**, across **Linux and Windows**, without restarting the game every time the
map changes. Shipped as ready-to-run binaries; built from a few small Python 3 modules with no
dependencies.

## Why this exists

Rocket League has a huge library of **custom/workshop maps**, but playing them **together with
friends** normally means fighting the game and bolting on a lot of extra software. `rpmap` makes
that painless:

- **Play from anywhere, with no L2 VPN.** Lobby discovery is a LAN broadcast, so the usual remote
  setups (Hamachi, ZeroTier) exist to fake a virtual LAN. `rpmap` works over a plain Layer-3 VPN
  (**Tailscale**) instead — it **relays Rocket League's LAN discovery** between peers, so the
  in-game lobby list works without an L2 network. On a real LAN it needs no VPN at all.
- **No BakkesMod needed.** The common way to host a custom-map lobby is Rocket Plugin, a
  BakkesMod plugin. `rpmap` supports that route if you want it, but its **slot swap + discovery
  relay** let you play **vanilla Rocket League** — no mods, no injector.
- **No game restart to change maps.** Rocket League only loads map packages at **startup**, so
  switching maps mid-session normally forces a restart. `rpmap` **overwrites a stock arena slot**,
  turning a map change into a plain file copy the running game picks up.
- **Easy to sync up.** Everyone still needs the same map file, but `rpmap` shows **who's online
  and which map each has loaded**, and can push/pull a switch for you — reference-only, so no map
  bytes are transferred.

The upshot: agree on a map, swap it in, host a lobby, and everyone joins from the in-game list over
the same Tailscale connection the game already uses.

## What it does

Three pieces, usable on their own:

1. **Slot swap (no restart).** Overwrites a stock arena package so the game loads your map when
   that arena is picked — backing up the stock file and swapping maps in and out. The slot is
   configurable (see [docs/usage.md](docs/usage.md)).
2. **Peer sync (reference-only).** Peers on the tailnet see what map each other has loaded (by
   slot, then `sha256`) and can push/pull a switch — no map bytes are transferred.
3. **Native LAN discovery (`rpmap lan`).** Relays Rocket League's LAN broadcast between tailnet
   peers so the in-game lobby list works; symmetric by default (everyone can host and join), with
   the explicit `lan-host` / `lan-join` pair still available.

## Get it

Packaged builds live on the [Releases](https://github.com/JThFr/rocket-league-workshop-map-swap/releases)
page — no Python needed:

- **Windows:** `rpmap.exe` (portable, single file)
- **Linux:** `rpmap-x86_64.AppImage` (runs on atomic distros like Bazzite with no install)

Or run it from source — it's one script, standard library only.

## Requirements

- **Rocket League** installed via **Steam** (primary, auto-detected) or the **Epic Games
  Store** (also auto-detected; see [docs/usage.md](docs/usage.md) to override the game path).
- **Windows 10/11** or a **Linux** desktop. The AppImage needs FUSE; it runs as-is on atomic
  distros like Bazzite.
- Running from **source** instead? Then you also need **Python 3** (standard library only —
  no `pip install`).

### Steam and Epic Games Store

The tool works with either store — both use the same
`rocketleague/TAGame/CookedPCConsole` layout, and BakkesMod/Rocket Plugin support both.

- **Steam (primary, what the project is built and tested against):** the install is found via
  `libraryfolders.vdf`, and **Workshop** subscriptions are picked up automatically (with
  previews and `steamcommunity.com` links in the UI).
- **Epic Games Store:** the install is found via Epic's launcher manifests under
  `%ProgramData%\Epic\...`, so a copy on another drive still works. Epic has **no Steam
  Workshop**, so the Workshop source simply doesn't apply — put downloaded `.udk`/`.upk` maps
  in `~/maps` (or the game's `mods/` folder) as usual. Maps without a Workshop id still sync
  by name+size and are verified by `sha256`.

Writing into `C:\Program Files\Epic Games\...` may need administrator rights, and either
store's update/"Verify" will restore the stock arena file — the tool's backup + hash detection
handles that. `RL_COOKED`/`--cooked` overrides detection for any custom location.

## Network modes — Tailscale or your own LAN

On first launch rpmap asks how you connect. **Tailscale is the recommended default**
and the mode the project is validated against; the alternative is there for people who
prefer their own network.

- **Tailscale (recommended, default).** A Layer-3 mesh VPN. Rocket League joins by
  **IP : port**, so Tailscale's stable `100.x` address works directly — no LAN broadcast
  needed, and NAT traversal is automatic. It also powers the automatic player list, and
  it's built into Bazzite. If Tailscale is already up, the choice is remembered and the
  prompt never appears; if it isn't, you're asked.
- **Local network / L2 VPN.** For a real LAN, or a Layer-2 VPN such as ZeroTier or
  Hamachi. Rocket League's own LAN discovery already works there, so the discovery relay
  is hidden and you add peers by IP. L2 VPNs need more setup and their broadcast/multicast
  can be inconsistent (MTU quirks, partial support); Hamachi is proprietary, caps free
  networks at 5 peers and is effectively unmaintained. That's why Tailscale is the
  recommendation.

Switch anytime in the web UI's **Connection** panel (or `rpmap network [tailscale|lan]`).
The mode only changes how peers are found and which addresses are accepted — map swapping
is identical either way. The server listens on all interfaces and **gates every request**:
Tailscale mode accepts only your tailnet (plus loopback), local-network mode accepts only
private/LAN addresses. Each call still needs the shared session code or the UI token.

## Updates — opt-in

A packaged build can update itself, but only if you say so. On first launch it asks how to
handle updates: **Off** (manual only), **Check on startup** (look for a newer release and tell
you, but never install it), or **Install automatically** — which means **trusting the owner of
the repository** to publish whatever they want (downloads are `sha256`-verified and only
accepted from this project's official GitHub releases). Until you choose, rpmap doesn't contact
GitHub. The web UI's **Updates** panel shows your version, the chosen mode, a **Check for
updates** button, and a link to the GitHub release. After an update, a packaged build shows a
**What's new** popup with the release notes for the versions it changed through (also reopenable
from the **Updates** panel). See
[docs/usage.md](docs/usage.md#updates) for the details.

## Usage

Everything you run — the visual interface, the full command reference, peer sync,
configuration, updates, and how to build the binaries — is documented in
**[docs/usage.md](docs/usage.md)**.

Starting it also opens a small **status window** ("rpmap is running", with Open UI / Restore
stock / Quit) so you can always tell it's up and stop it without a terminal — useful after a
self-update, which runs the app detached. `--no-window` turns it off. The window and the
browser tab both show the rpmap icon, and the UI's **Logs** panel surfaces the server output a
detached run would otherwise hide.

Something not working? **`rpmap doctor`** prints a health report — game path, slot + backup,
Tailscale, session code, self-update TLS, the status window, and which tailnet peers are actually
serving rpmap.

## The swap, and the rules

- In Free Play / Custom Games, pick the arena that matches your **slot**. The default is a
  Rocket Labs arena that online playlists don't use; the slot is configurable — see
  [docs/usage.md](docs/usage.md) for the arena names.
- Keep the **same map file on every player** (a mismatch = content mismatch / crash).
- **Restore the stock map before playing online** if your slot arena is one that online uses
  (Arcade/LTM rotates Rocket Labs arenas). The UI's "Restore stock map" button does this, and
  `--restore-on-exit` does it automatically when you quit (the UI's **Quit** button, or
  `rpmap stop`).
- The stock map is backed up next to the slot (e.g. `Labs_Utopia_P.upk.orig`); the tools also
  recognise the `ORIGINAL_*.upk` names other loaders use.
- A pure file swap is not injection, but Rocket League mods generally want a **no-EAC
  launch** — safe default.

## Peer sync over Tailscale (Phase 2)

Each machine runs its own `rpmap` server (listening on all interfaces, gated to the
tailnet in Tailscale mode — no `--host` needed) and shares a session code.
The **Players** panel then shows who's online and what each has loaded, with **Switch me** /
**Set theirs** / **Load mine on everyone** actions and an **Only maps everyone has** filter.
In local-network mode the roster is your manually-added peers instead of the tailnet. Syncs are
**explicit** and reference-only — no map bytes move; anything obtained outside the Steam Workshop
syncs only if the peer already has the byte-identical file. Never swap during an online match.

Sync state is **slot-aware**: two players on the same *stock* arena count as in sync (only custom
maps are byte-compared), so build/platform differences in the stock `.upk` don't raise a false
mismatch. Each poll also measures the **round-trip time** to every reachable peer — the table's
**Ping** column — and the lowest-latency node gets a **★ host** badge as the recommended host.

## Documentation

- [docs/usage.md](docs/usage.md) — running it, commands, peer sync, configuration, updates, building
- [docs/rpmap-phase2.md](docs/rpmap-phase2.md) — peer-sync design
- [maps/README.md](maps/README.md) — where to put map files

## Native LAN discovery over Tailscale

Rocket League's **Create LAN / Join LAN** list is driven by a broadcast that a normal VPN hides.
`rpmap` can relay that discovery across the tailnet (this is **only needed for Tailscale** — a
real LAN or an L2 VPN carries the broadcast itself):

```bash
# run on every machine (symmetric — anyone can host and join at once):
python3 rpmap.py lan

# or the explicit-role pair:
#   host:    python3 rpmap.py lan-host
#   joiner:  python3 rpmap.py lan-join --host <host-tailnet-ip>
```

All sides use the shared **session code** (the relay token defaults to it). Then open the game's
**Join LAN / local lobby** list — the host's lobby appears and connects straight to the host's
`100.x:7777`. The relay carries **only discovery**; the match itself runs over Tailscale.
Validated in-game between a Linux host and a Windows peer.

## License

MIT — see [LICENSE](LICENSE).

### Not affiliated with Psyonix or Epic Games

`rpmap` is an unofficial, non-commercial community tool. It is **not affiliated with, endorsed
by, or sponsored by** Psyonix LLC or Epic Games. "Rocket League" and the Rocket League logo are
trademarks of Psyonix LLC, used here only to describe what the tool is for.

The app icon (`assets/rpmap.png`) is **original artwork** — a generic hex-panelled sphere, not a
Rocket League asset. It can be regenerated with `scripts/make_icon.py`.
