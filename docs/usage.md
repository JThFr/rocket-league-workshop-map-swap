# Usage

`rpmap` is a single script, `scripts/rpmap.py`. On Linux, `scripts/rpmap` is a thin launcher
for it; on Windows run `python rpmap.py <command>` — the arguments are identical.

## Run it

```bash
python3 rpmap.py                 # visual interface (opens a local web UI in your browser)
```

On Linux the launcher form works too:

```bash
scripts/rpmap list
```

## Commands

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
| `python3 rpmap.py serve [--host H --port P] [--restore-on-exit]` | run the web UI / peer server |
| `python3 rpmap.py session [--new]` · `join <code>` · `name <display>` | shared session code / identity |
| `python3 rpmap.py version` | print the version |
| `python3 rpmap.py check-update` | check GitHub for a newer release |

Global options: `--cooked <path>` (override the game's `CookedPCConsole`), `--slot <name>`,
`--no-update-check`.

### Slot & arena

`--slot` picks which stock arena package gets overwritten, and which arena you select in Free
Play:

- `utopia` (**default**) → `Labs_Utopia_P.upk` (arena **Utopia Retro**) — not in online playlists
- `underpass` → `Labs_Underpass_P.upk` (arena **Underpass**) — used in Rumble; not "Tokyo Underpass"

`serve --restore-on-exit` puts the stock map back when the program exits cleanly (Ctrl+C or
closing the terminal); the UI's "Restore stock map" button does it on demand.

## Web UI

`python3 rpmap.py` starts a small local HTTP server and opens a browser page where you can
browse maps, see the current slot, swap/restore, and (over Tailscale) view peers and sync
switches. It prints a `127.0.0.1` URL with a token.

Every map gets a preview: the map's own image when it ships one, otherwise fetched once from
Steam's public workshop API and cached.

## Peer sync over Tailscale

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

Design notes: [`rpmap-phase2.md`](rpmap-phase2.md).

## Configuration

- `RL_COOKED` — override the `TAGame/CookedPCConsole` path.
- `RL_MAPS` — extra folder of `.udk`/`.upk` maps (default `~/maps`).
- Per-user config lives in `%APPDATA%\rpmap\config.json` (Windows) or
  `~/.config/rpmap/config.json` (Linux); preview cache in the matching local cache dir.

## Updates

A packaged build checks GitHub Releases **on startup**. If a newer version exists it downloads
the matching asset (`rpmap.exe` / `rpmap-x86_64.AppImage`) and replaces itself:

- **Windows:** a tiny helper waits for the process to exit, swaps the `.exe`, and relaunches.
- **Linux (AppImage or binary):** the file is replaced in place and the new build is started.

Running from source (the `.py`) never self-updates — it only prints the available version.
Skip the startup check with `--no-update-check`, or run it manually with `check-update`.

## Versioning & releases

The version lives in the repo's **`VERSION`** file — the single source of truth. The app reads it
at runtime (packaged builds bundle it), so the UI header and the `version` command always match
the release tag.

Releases are automatic: a push/merge to `main` runs the `release` workflow, which reads `VERSION`
and, if the tag `v<VERSION>` doesn't already exist, builds both binaries and creates a GitHub
Release tagged `v<VERSION>`.

**Bump `VERSION` in every PR** — a CI check (`version-check`) fails a pull request whose `VERSION`
is not a valid increase over `main`, for example `0.3.1 → 0.3.2` (patch), `0.3.1 → 0.4.0` (minor),
`0.4.0 → 1.0.0` (major).

## Building locally

```bash
scripts/build-appimage.sh      # Linux AppImage
```

```powershell
scripts\build-windows.ps1      # Windows .exe
```

Both use PyInstaller (`--onefile --console`, bundling `VERSION`). The Linux AppImage wraps the
binary in an AppDir (`assets/rpmap.desktop` + icon) with `appimagetool`.

## Tests

Standard library only:

```bash
python3 -m unittest discover -s tests -v
```

CI (`.github/workflows/ci.yml`) runs these plus the Windows and Linux builds on every pull
request; all checks must pass before merging to `main`.
