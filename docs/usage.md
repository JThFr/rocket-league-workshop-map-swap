# Usage

`rpmap` is a single script, `scripts/rpmap.py`. On Linux, `scripts/rpmap` is a thin launcher
for it; on Windows run `python rpmap.py <command>` — the arguments are identical.

## Run it

```bash
python3 rpmap.py                 # status window + local web UI in your browser
```

On Linux the launcher form works too:

```bash
scripts/rpmap list
```

### The status window

Alongside the web UI, rpmap shows a small **status window** — "rpmap is running", the current
slot, and **Open UI** / **Restore stock** / **Quit** buttons — so you can always see it's up and
stop it without a terminal. This matters after a **self-update**, when the app relaunches
**detached** (no terminal, no Ctrl+C).

- `--no-window` disables it (e.g. a headless/peer-only server).
- It needs Tk + a display. Packaged builds (AppImage / `.exe`) bundle Tcl/Tk, so it always works
  there. Running from **source**, your Python needs `tkinter` (Debian/Ubuntu: `python3-tk`,
  Fedora: `python3-tkinter`); if it's missing, rpmap just runs **headless** — no error.

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
| `python3 rpmap.py serve [--host H --port P] [--network tailscale\|lan] [--restore-on-exit]` | run the web UI / peer server |
| `python3 rpmap.py lan-host [--peer IP]...` | relay native LAN discovery (you host the LAN match) |
| `python3 rpmap.py lan-join --host IP` | receive relayed native LAN discovery (you join) |
| `python3 rpmap.py session [--new]` · `join <code>` · `name <display>` | shared session code / identity |
| `python3 rpmap.py network [tailscale\|lan]` | show or set the peer connection mode |
| `python3 rpmap.py stop` | stop a running web UI (same as its Quit button) |
| `python3 rpmap.py doctor` | diagnose the setup (game path, slot, Tailscale, peers, TLS, window) |
| `python3 rpmap.py version` | print the version |
| `python3 rpmap.py check-update` | check GitHub for a newer release |

Global options: `--cooked <path>` (override the game's `CookedPCConsole`), `--slot <name>`,
`--no-update-check`, `--no-window`, `--restore-on-exit`.

### Slot & arena

`--slot` picks which stock arena package gets overwritten, and which arena you select in Free
Play:

- `utopia` (**default**) → `Labs_Utopia_P.upk` (arena **Utopia Retro**) — not in online playlists
- `underpass` → `Labs_Underpass_P.upk` (arena **Underpass**) — used in Rumble; not "Tokyo Underpass"

`--restore-on-exit` is a global flag (so it also works with the no-argument UI launch, e.g.
`./rpmap-x86_64.AppImage --restore-on-exit`) and puts the stock map back when the program exits
cleanly — quit from the UI (the header's **Quit** button), `rpmap stop`, Ctrl+C, or closing the
terminal. The UI's "Restore stock map" button does it on demand.

## Web UI

`python3 rpmap.py` starts a small local HTTP server and opens a browser page where you can
browse maps, see the current slot, swap/restore, and (over Tailscale) view peers and sync
switches. It prints a URL with a token.

To stop it, use the header's **Quit** button, the **status window's** Quit button, or run
`rpmap stop` from a terminal — handy after a self-update, when the AppImage relaunches detached
with no terminal to Ctrl+C.

Only **one** rpmap UI runs at a time: if you launch it again while one is already up, it reopens
the running one instead of starting a second server.

Every map gets a preview: the map's own image when it ships one, otherwise fetched once from
Steam's public workshop API and cached.

## Connection modes

rpmap reaches peers one of two ways; the **Connection** panel in the web UI (or
`python3 rpmap.py network [tailscale|lan]`) switches between them. **Tailscale is the
recommended default** and the mode the project is validated on.

- **Tailscale.** Discovers the tailnet roster automatically and gates the peer API to
  `100.64.0.0/10`. If Tailscale is up, rpmap remembers this and never asks again; if it
  isn't, the first launch prompts for a choice.
- **Local network / L2 VPN** (a real LAN, ZeroTier, Hamachi). No tailnet: peers are the
  ones you add by IP in the **Players** panel, and the API accepts private/LAN addresses.
  Rocket League's own LAN discovery already works here, so the relay below is hidden.

Either way the server listens on all interfaces (`--host` overrides) and gates each
request by source address, plus the session code / UI token. The mode changes only how
peers are found and which addresses are accepted — swapping maps is identical.

## Peer sync over Tailscale

Each machine runs its own `rpmap` server (it listens on all interfaces; you no longer need
to bind it to the tailnet IP yourself) and shares a session code, which authorises the API
calls.

```bash
# on each machine
python3 rpmap.py
# it prints a session code — share it; others paste it into the UI's "Join" box
#   (or: python3 rpmap.py join <code>  /  python3 rpmap.py name "Julien")
python3 rpmap.py peers      # text roster: who's online, what they've loaded, in-sync
```

In the UI's **Players** panel: **Switch me** (load a peer's map here), **Set theirs** (load
your map on one peer), **Load mine on everyone** (push to all reachable peers). A peer that
doesn't have the map reports *not installed* with a Workshop link. Syncs are **explicit** and
reference-only — anything obtained outside the Steam Workshop syncs only if the peer already
has the byte-identical file. Never swap during an online match.

Sync requests are validated: a peer's rpmap only contacts addresses it discovered on its
tailnet roster (or its configured `manual_peers`) — anything else is refused as *not a known
peer*. The session code can only be rotated from the machine itself (the UI's **Join** box or
`rpmap join`); peers can sync maps with you, but can never change your session code or stop
your server.

The map grid has an **Only maps everyone has** toggle that hides maps a reachable peer would
have to download. "Everyone" includes **you**, so with no peers running rpmap it simply shows
all your maps (the toggle is still usable).

Design notes: [`rpmap-phase2.md`](rpmap-phase2.md).

The **Players** panel labels a tailnet node that isn't usable: *offline*, *online · not rpmap*
(Tailscale is up there but rpmap isn't running / isn't bound to the tailnet), or *rpmap · no map
list* (an older build). "In sync" counts only the reachable rpmap peers.

## Native LAN discovery over Tailscale

Rocket League's **Create LAN / Join LAN** list uses a broadcast the game sends to
`255.255.255.255:14001`, which never leaves the local network — so tailnet peers don't see each
other's lobbies. `rpmap` can **relay** that discovery between tailnet peers, giving you the
in-game lobby browser without BakkesMod/Rocket Plugin. The relay is **only for Tailscale** —
a real LAN or an L2 VPN carries the broadcast itself, so it's hidden in local-network mode:

```bash
# on the machine hosting the LAN match
python3 rpmap.py lan-host
# on every joiner (host's tailnet IP)
python3 rpmap.py lan-join --host 100.x.y.z
```

Then: the host **creates a LAN match**; each joiner opens the **Join LAN / local lobby** list, the
host's lobby appears, and clicking it **connects directly to the host's `100.x:7777`** over
Tailscale. Only the discovery is relayed — the match itself is a normal Tailscale connection.

You can also run it entirely from the **web UI's "LAN discovery relay" panel**: choose
**I'm hosting** or **I'm joining** (enter the host's tailnet IP), then **Start relay** / **Stop**.
The panel shows whether it's relaying, and quitting rpmap stops it.

- The shared **session code** is the relay token by default (`~/.config/rpmap/config.json`, or
  `rpmap session`), so both sides just need the same code; `--token` overrides.
- `lan-host` targets **all online tailnet peers** by default (`--peer <ip>` to restrict).
- The relay binds to this machine's **tailnet IP** and drops packets from non-tailnet sources;
  `--bind`, `--relay-port` (default `14002`), `--inject broadcast|loopback`, `--lan-port`
  (default `14001`) and `--advertise <ip>` (host: the address written into the lobby reply) are
  available when needed.
- Keep **no-EAC** launches, as with any mod-adjacent tooling.
- The relay was validated in-game between a Linux host and a Windows peer. Prefer a **direct**
  tailnet path over a DERP relay for latency.

## Troubleshooting

`python3 rpmap.py doctor` prints a one-shot report of the usual suspects — where Rocket League
was found, the slot's state and whether a stock backup exists, whether the UI is running, the
connection mode and address, the session code, the TLS trust store (self-update), whether the
status window can show, and how many peers are actually serving rpmap:

```
rpmap doctor
  [ok] Rocket League: /…/TAGame/CookedPCConsole
  [ok] slot: Utopia Retro: stock map [stock]
  [ok] network: Tailscale (Julien) 100.108.219.85
  [!!] peers: 1 on the tailnet, 0 serving rpmap
         -> start rpmap there with `serve --host <tailscale-ip>`
```

Exit code is non-zero only when a hard check fails (e.g. Rocket League not found).

## Configuration

- `RL_COOKED` — override the `TAGame/CookedPCConsole` path.
- `RL_MAPS` — extra folder of `.udk`/`.upk` maps (default `~/maps`).
- Per-user config lives in `%APPDATA%\rpmap\config.json` (Windows) or
  `~/.config/rpmap/config.json` (Linux); preview cache in the matching local cache dir.
  It holds `network_mode` (`tailscale` or `lan`), the `session_secret`, the display
  `name`, and any `manual_peers` (used in local-network mode).
- While the UI is running it also writes `runtime.json` next to the config (mode `0600`, removed
  on a clean exit) so `rpmap stop` and a second launch can find the live server. It holds this
  run's UI token, which is regenerated every time the server starts.

## Updates

A packaged build checks GitHub Releases **on startup**. If a newer version exists it downloads
the matching asset (`rpmap.exe` / `rpmap-x86_64.AppImage`) and replaces itself:

- **Windows:** a tiny helper waits for the process to exit, swaps the `.exe`, and relaunches.
- **Linux (AppImage or binary):** the file is replaced in place and the new build is started.

Running from source (the `.py`) never self-updates — it only prints the available version.
Skip the startup check with `--no-update-check`, or run it manually with `check-update`.

The relaunched build is detached (no terminal), so once it's up you stop it with the UI's
**Quit** button or `rpmap stop` — not Ctrl+C.

## Versioning & releases

The version lives in the repo's **`VERSION`** file — the single source of truth. The app reads it
at runtime (packaged builds bundle it), so the UI header and the `version` command always match
the release tag.

Releases are automatic: a push/merge to `main` runs the `release` workflow, which reads `VERSION`
and, if the tag `v<VERSION>` doesn't already exist, builds both binaries and creates a GitHub
Release tagged `v<VERSION>`. That same run then **prunes older releases**: only the newest **10**
are kept (each carries both assets, ~32 MB). The **git tags are kept**, so version history and
rollback links survive — only the release and its downloadable assets are removed.

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
