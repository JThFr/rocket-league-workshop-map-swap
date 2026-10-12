# rocket-league-workshop-swap

**`rpmap`** — a helper to play **custom / workshop maps with friends in a Rocket League
multiplayer lobby**, across **Linux and Windows**, without restarting the game every time the
map changes. One Python 3 script, standard library only.

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
   cross the internet. **Tailscale** — a mesh VPN — gives everyone a
   stable `100.x.y.z` address and a direct path, so the IP join just works. That's what the
   Tailscale parts are for: the same tailnet is also how the **peer sync** sees who has which
   map loaded.

## What it does

1. **Slot swap (no restart).** Rocket League loads the stock `Labs_Underpass_P.upk` whenever
   you pick the arena **Underpass** (not "Tokyo Underpass"). Overwrite that one file and the
   game loads your map — no plugin, no restart. `rpmap` backs up the stock file and swaps
   maps in and out.
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

Or run it from source — it's one script, standard library only (see below).

## Requirements

- **Python 3** (the script uses only the standard library — no `pip install` needed).
- Rocket League installed via **Steam**. Auto-detected on Linux and Windows; override with
  `--cooked <path>` or the `RL_COOKED` env var if detection misses.

## Run it

```bash
python3 rpmap.py                 # visual interface (opens a local web UI in your browser)
```

On Linux, `scripts/rpmap` is a thin launcher for the same script, so both forms work:

```bash
scripts/rpmap list
```

On Windows: `python rpmap.py <command>` (same arguments).

### Commands

| Command | What it does |
|---|---|
| `python3 rpmap.py` | visual interface (local web UI) |
| `python3 rpmap.py list` | candidate maps (workshop / `mods/` / `~/maps`) |
| `python3 rpmap.py current` | what is loaded in the slot |
| `python3 rpmap.py use <name\|path>` | swap a map in (backs up the stock file first) |
| `python3 rpmap.py restore` | restore the stock map |
| `python3 rpmap.py install <file>` | stage a map for Rocket Plugin |
| `python3 rpmap.py status <host> [port]` | ask a Rocket Plugin host what map it serves |
| `python3 rpmap.py fetch <host> [port]` | download the host's map and stage it |
| `python3 rpmap.py peers` | tailnet peers and the map each has loaded |
| `python3 rpmap.py serve [--host H --port P]` | run the web UI / peer server |
| `python3 rpmap.py session [--new]` · `join <code>` · `name <display>` | shared session code / identity |
| `python3 rpmap.py version` | print the version |
| `python3 rpmap.py check-update` | check GitHub for a newer release |

### Web UI

`python3 rpmap.py` starts a small local HTTP server and opens a browser page where you can
browse maps, see the current slot, swap/restore, and (over Tailscale) view peers and sync
switches. It prints a `127.0.0.1` URL with a token.

Every map gets a preview: the map's own image when it ships one, otherwise fetched once from
Steam's public workshop API and cached.

## The swap, and the rules

- Pick the arena **"Underpass"** in Free Play / Custom Games — **not** "Tokyo Underpass".
  They are different packages (`Labs_Underpass_P.upk` vs `Labs_Underpass_v0_p.upk`).
- Keep the **same map file on every player** (a mismatch = content mismatch / crash).
- **Restore the stock map before playing online** — Rumble uses Underpass. The UI has a
  "Restore stock Underpass" button, and `restore` does the same from the CLI.
- The stock map is backed up next to the slot as `Labs_Underpass_P.upk.orig`; the tools also
  recognise the `ORIGINAL_UNDERPASS.upk` name other loaders use.
- A pure file swap is not injection, but Rocket League mods generally want a **no-EAC
  launch** — safe default.

## Peer sync over Tailscale (Phase 2)

Each machine runs its own `rpmap` server bound to its tailnet IP; a shared session code
authorises the API calls.

```bash
# on each machine (bind to your tailnet IP, not 0.0.0.0)
python3 rpmap.py serve --host "$(tailscale ip -4)"
# it prints a session code — share it; others paste it into the UI's "Join" box
#   (or: python3 rpmap.py join <code>  /  python3 rpmap.py name "Julien")
python3 rpmap.py peers      # text roster: who's online, what they've loaded, in-sync
```

In the UI's **Players** panel: **Switch me** (load a peer's map here), **Set theirs** (load
your map on one peer), **Load mine on everyone** (push to all reachable peers). A peer that
doesn't have the map reports *not installed* with a Workshop link. Syncs are **explicit** and
reference-only — anything obtained outside the Steam Workshop syncs only if the peer already
has the byte-identical file. Never swap during an online match.

Design notes: [`docs/rpmap-phase2.md`](docs/rpmap-phase2.md).

## Updates

A packaged build checks GitHub Releases **on startup**. If a newer version exists it downloads
the matching asset (`rpmap.exe` / `rpmap-x86_64.AppImage`) and replaces itself:

- **Windows:** a tiny helper waits for the process to exit, swaps the `.exe`, and relaunches.
- **Linux (AppImage or binary):** the file is replaced in place and the new build is started.

Running from source (the `.py`) never self-updates — it only prints the available version.
Skip the startup check with `--no-update-check`, or run it manually with `check-update`.

## Building a release

CI (`.github/workflows/release.yml`) builds both artifacts on a `v*` tag and attaches them to a
GitHub release. The tag must match `APP_VERSION` in `scripts/rpmap.py`.

Locally:

```bash
scripts/build-appimage.sh      # Linux AppImage
```

```powershell
scripts\build-windows.ps1      # Windows .exe
```

Both use PyInstaller (`--onefile --console`). The Linux AppImage wraps the binary in an AppDir
(`assets/rpmap.desktop` + icon) with `appimagetool`.

## Tests

Standard library only:

```bash
python3 -m unittest discover -s tests -v
```

CI (`.github/workflows/ci.yml`) runs these plus the Windows and Linux builds on every pull
request; all checks must pass before merging to `main`.

## Configuration

- `RL_COOKED` — override the `TAGame/CookedPCConsole` path.
- `RL_MAPS` — extra folder of `.udk`/`.upk` maps (default `~/maps`).
- Per-user config lives in `%APPDATA%\rpmap\config.json` (Windows) or
  `~/.config/rpmap/config.json` (Linux); preview cache in the matching local cache dir.

## Maps

See [`maps/README.md`](maps/README.md) for where to put map files.
