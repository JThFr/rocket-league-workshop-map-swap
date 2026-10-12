# rocket-league-workshop-swap

**`rpmap`** — a helper to play **custom / workshop maps with friends in a Rocket League
multiplayer lobby**, across **Linux and Windows**, without restarting the game every time the
map changes. Shipped as ready-to-run binaries; built from a single Python 3 script with no
dependencies.

## Why this exists

The way we play: one person **hosts a lobby with Rocket Plugin** (a BakkesMod plugin) so
friends can join a custom-map game, and everyone plays the same workshop map. Two things get
in the way, and this tool exists to smooth them out:

1. **Rocket Plugin makes everyone restart to load a new map.** Rocket League only loads map
   packages at **game startup**, so when a client doesn't already have the selected map,
   Rocket Plugin's only option is **"Copy Map and Restart"**. `rpmap` avoids the restart where
   it can — by **staging maps** into the folders Rocket Plugin uses *before* the game
   launches, and (the headline trick) by **swapping a stock arena slot** so changing the map
   is just a file copy the running game picks up — no restart at all.
2. **Your friends aren't on your LAN.** Rocket Plugin lobbies are joined by **IP : port**
   (UDP 7777), and the built-in "Local Match" discovery needs LAN broadcast, which doesn't
   cross the internet. **Tailscale** — a mesh VPN — gives everyone a stable `100.x.y.z`
   address and a direct path, so the IP join just works. That's what the Tailscale parts are
   for: the same tailnet is also how the **peer sync** sees who has which map loaded.

## What it does

1. **Slot swap (no restart).** Rocket League loads a stock arena package whenever you pick
   that arena. Overwrite that one file and the game loads your map — no plugin, no restart.
   `rpmap` backs up the stock file and swaps maps in and out; which arena slot it uses is
   configurable (see [docs/usage.md](docs/usage.md)).
2. **Rocket Plugin staging.** Copy maps into `CookedPCConsole/{mods,rocketplugin}` so Rocket
   Plugin sees them without its "Copy Map and Restart" step.
3. **Peer sync (reference-only).** Peers on a Tailscale tailnet see what map each other has
   loaded (compared by `sha256`) and can push/pull a switch. No map bytes are transferred —
   each peer loads the map from its own copy.

## Get it

Packaged builds live on the [Releases](https://github.com/JThFr/rocket-league-workshop-map-swap/releases)
page — no Python needed:

- **Windows:** `rpmap.exe` (portable, single file)
- **Linux:** `rpmap-x86_64.AppImage` (runs on atomic distros like Bazzite with no install)

Or run it from source — it's one script, standard library only.

## Requirements

- **Rocket League** installed via **Steam** (auto-detected; see
  [docs/usage.md](docs/usage.md) to override the game path).
- **Windows 10/11** or a **Linux** desktop. The AppImage needs FUSE; it runs as-is on atomic
  distros like Bazzite.
- Running from **source** instead? Then you also need **Python 3** (standard library only —
  no `pip install`).

## Usage

Everything you run — the visual interface, the full command reference, peer sync,
configuration, updates, and how to build the binaries — is documented in
**[docs/usage.md](docs/usage.md)**.

Starting it also opens a small **status window** ("rpmap is running", with Open UI / Restore
stock / Quit) so you can always tell it's up and stop it without a terminal — useful after a
self-update, which runs the app detached. `--no-window` turns it off.

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

Each machine runs its own `rpmap` server bound to its tailnet IP and shares a session code.
The **Players** panel then shows who's online and what each has loaded (compared by `sha256`),
with **Switch me** / **Set theirs** / **Load mine on everyone** actions. Syncs are **explicit**
and reference-only — no map bytes move; anything obtained outside the Steam Workshop syncs
only if the peer already has the byte-identical file. Never swap during an online match.

## Documentation

- [docs/usage.md](docs/usage.md) — running it, commands, peer sync, configuration, updates, building
- [docs/rpmap-phase2.md](docs/rpmap-phase2.md) — peer-sync design
- [maps/README.md](maps/README.md) — where to put map files
