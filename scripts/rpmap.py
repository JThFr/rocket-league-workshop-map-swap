#!/usr/bin/env python3
"""rpmap - Rocket League custom-map helper (Linux + Windows). One script.

Two independent jobs:

  1. "slot swap" (no mods, no restart)
     Rocket League always loads the stock arena Labs_Underpass_P.upk when you
     pick arena **Underpass** (NOT "Tokyo Underpass"). Overwrite that one file
     and the game loads your custom map - no BakkesMod, no Rocket Plugin, no
     restart. This tool backs up the stock file and swaps maps in and out.

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
import concurrent.futures
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

# slot key -> (file RL loads, alternate "original" backup name other tools use)
SLOTS = {
    "underpass": ("Labs_Underpass_P.upk", "ORIGINAL_UNDERPASS.upk"),
    "utopia": ("Labs_Utopia_P.upk", "ORIGINAL_UTOPIA.upk"),
}
DEFAULT_SLOT = "underpass"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".jfif", ".webp"}
TITLE_RE = re.compile(r'"title"\s+"([^"]*)"')
PATH_RE = re.compile(r'"path"\s+"([^"]*)"')

_hash_cache: dict[tuple[str, int, int], str] = {}
_preview_url_cache: dict[str, str] = {}


# --------------------------------------------------------------------------- #
# Config file / identity
# --------------------------------------------------------------------------- #

PRODUCT = "rpmap"
APP_VERSION = "0.3.1"
DEFAULT_PORT = 8787
TAILNET = ipaddress.ip_network("100.64.0.0/10")

# Where the packaged builds live (used only by the self-updater).
GITHUB_REPO = "JThFr/rocket-league-workshop-map-swap"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"


def config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData/Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "rpmap" / "config.json"


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2))
    if os.name != "nt":
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass


def new_secret() -> str:
    """A short, human-typeable session code (64 bits, grouped 4-4-4-4)."""
    h = secrets.token_hex(8)
    return "-".join(h[i:i + 4] for i in range(0, 16, 4))



# --------------------------------------------------------------------------- #
# Self-update (GitHub releases)
# --------------------------------------------------------------------------- #

def _is_frozen() -> bool:
    """True when running from a packaged build rather than the .py source."""
    return bool(getattr(sys, "frozen", False))


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", text or "")
    return tuple(int(p) for p in parts) if parts else (0,)


def _update_asset_name() -> str:
    return "rpmap.exe" if os.name == "nt" else "rpmap-x86_64.AppImage"


def _update_target() -> tuple[Path, str] | None:
    """(path to replace, kind) for the running build, or None if not packaged.

    ``kind`` is ``"exe"`` (Windows - needs the helper swap) or ``"file"``
    (Linux binary / AppImage - can be replaced in place).
    """
    if os.name == "nt":
        return (Path(sys.executable), "exe") if _is_frozen() else None
    appimage = os.environ.get("APPIMAGE")
    if appimage and Path(appimage).is_file():
        return (Path(appimage), "file")
    return (Path(sys.executable), "file") if _is_frozen() else None


def _latest_release(timeout: float = 5.0) -> dict | None:
    req = urllib.request.Request(GITHUB_RELEASES_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{PRODUCT}/{APP_VERSION}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def check_for_update() -> dict | None:
    """Return release info if GitHub publishes a newer tag, else None."""
    rel = _latest_release()
    if not rel:
        return None
    tag = str(rel.get("tag_name") or "").strip()
    if not tag or _version_tuple(tag) <= _version_tuple(APP_VERSION):
        return None
    asset = _update_asset_name()
    url, digest, size = GITHUB_RELEASES_PAGE, None, 0
    for a in rel.get("assets") or []:
        if str(a.get("name") or "").lower() == asset.lower():
            url = a.get("browser_download_url") or url
            digest = a.get("digest")            # "sha256:<hex>" on newer GitHub API
            size = int(a.get("size") or 0)
            break
    return {"tag": tag, "version": tag.lstrip("vV"), "url": url,
            "digest": digest, "size": size}


def _download_update(url: str, dest: Path, digest: str | None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        for chunk in iter(lambda: r.read(1024 * 1024), b""):
            h.update(chunk)
            f.write(chunk)
    if digest:
        want = digest.split(":")[-1].lower()
        if want and h.hexdigest().lower() != want:
            try:
                dest.unlink()
            except OSError:
                pass
            raise RuntimeError("downloaded update failed the sha256 check")


def _spawn_windows_swap(new_exe: Path, current_exe: Path) -> None:
    """Write a .cmd that waits for this process to exit, swaps the exe, relaunches.

    A running .exe can't be overwritten on Windows, so retry ``move`` until the
    file unlocks, then start the new build. ``ping`` is the delay because
    ``timeout`` needs a console, which a detached process doesn't have.
    """
    cmd = Path(tempfile.gettempdir()) / "rpmap-update.cmd"
    lines = [
        "@echo off",
        "setlocal",
        "set N=0",
        ":retry",
        f'move /y "{new_exe}" "{current_exe}" >nul 2>&1',
        "if not errorlevel 1 goto replaced",
        "set /a N+=1",
        "if %N% geq 120 exit /b 1",
        "ping -n 2 127.0.0.1 >nul",
        "goto retry",
        ":replaced",
        f'start "" "{current_exe}"',
        'del "%~f0"',
        "",
    ]
    cmd.write_text("\r\n".join(lines))
    detached = 0x00000008          # DETACHED_PROCESS
    new_group = 0x00000200         # CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(["cmd", "/c", str(cmd)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True,
                     creationflags=detached | new_group)


def apply_update(info: dict) -> None:
    """Download and install a newer build, then restart. Only for packaged builds."""
    url = info.get("url")
    target = _update_target()
    if not url or not target:
        return
    path, kind = target
    staged = path.with_name(path.name + ".new")
    try:
        _download_update(url, staged, info.get("digest"))
    except Exception as e:
        print(f"rpmap: update download failed: {e}", file=sys.stderr)
        return
    if kind == "exe":
        _spawn_windows_swap(staged, path)
        print(f"rpmap: updating to {info.get('version')} - restarting...", flush=True)
        os._exit(0)
    try:                       # POSIX can replace the running file in place
        os.chmod(staged, 0o755)
        os.replace(staged, path)
    except OSError as e:
        print(f"rpmap: update swap failed: {e}; get it from {GITHUB_RELEASES_PAGE}",
              file=sys.stderr)
        return
    print(f"rpmap: updated to {info.get('version')} - restarting...", flush=True)
    os.execv(str(path), [str(path), *sys.argv[1:]])


def maybe_self_update(auto: bool) -> None:
    """Check GitHub for a newer release. If ``auto``, install it when packaged."""
    info = check_for_update()
    if not info:
        return
    if auto and _update_target():
        apply_update(info)
        return
    print(f"rpmap: newer version available: {info['tag']}  ->  {info['url']}",
          file=sys.stderr)


# --------------------------------------------------------------------------- #
# Install detection
# --------------------------------------------------------------------------- #

def _steam_libraries() -> list[Path]:
    libs: list[Path] = []
    steam_roots = [
        Path.home() / ".steam/steam",
        Path.home() / ".local/share/Steam",
        Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        Path("C:/Program Files (x86)/Steam"),
        Path("C:/Program Files/Steam"),
    ]
    for root in steam_roots:
        if not root.is_dir():
            continue
        libs.append(root)
        vdf = root / "steamapps/libraryfolders.vdf"
        if vdf.is_file():
            try:
                for m in PATH_RE.finditer(vdf.read_text(errors="ignore")):
                    libs.append(Path(m.group(1).replace("\\\\", "\\")))
            except OSError:
                pass
    return libs


def detect_cooked(override: str | None = None) -> Path | None:
    if override:
        p = Path(override)
        return p if p.is_dir() else None
    env = os.environ.get("RL_COOKED")
    if env and Path(env).is_dir():
        return Path(env)

    candidates: list[Path] = [
        Path("/var/mnt/storage/SteamLibrary/steamapps/common/rocketleague/TAGame/CookedPCConsole"),
        Path.home() / ".steam/steam/steamapps/common/rocketleague/TAGame/CookedPCConsole",
        Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common/rocketleague/TAGame/CookedPCConsole",
    ]
    for lib in _steam_libraries():
        candidates.append(lib / "steamapps/common/rocketleague/TAGame/CookedPCConsole")
    candidates.append(Path("C:/Program Files/Epic Games/rocketleague/TAGame/CookedPCConsole"))
    for c in candidates:
        if c.is_dir():
            return c
    return None


def steamapps_from_cooked(cooked: Path) -> Path | None:
    try:
        sa = cooked.parents[3]
        return sa if sa.is_dir() else None
    except IndexError:
        return None


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #

def file_hash(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    key = (str(path), st.st_size, int(st.st_mtime))
    if key in _hash_cache:
        return _hash_cache[key]
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return ""
    _hash_cache[key] = h.hexdigest()
    return _hash_cache[key]


def _map_id(path: str) -> str:
    return hashlib.md5(path.encode("utf-8", "replace")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Previews (local image, else Steam's public preview API, cached)
# --------------------------------------------------------------------------- #

def _cache_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    d = base / "rpmap" / "previews"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _find_local_preview(folder: Path) -> Path | None:
    try:
        imgs = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    except OSError:
        return None
    if not imgs:
        return None

    def score(p: Path):
        n = p.name.lower()
        named = 0 if ("preview" in n or "thumb" in n) else 1
        try:
            return (named, -p.stat().st_size)
        except OSError:
            return (named, 0)

    return sorted(imgs, key=score)[0]


def _sniff_image(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _steam_preview_url(published_id: str) -> str | None:
    if published_id in _preview_url_cache:
        return _preview_url_cache[published_id] or None
    try:
        data = urllib.parse.urlencode(
            {"itemcount": 1, "publishedfileids[0]": published_id}
        ).encode()
        req = urllib.request.Request(
            "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/",
            data=data,
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            payload = json.loads(r.read())
        details = (payload.get("response", {}).get("publishedfiledetails") or [{}])[0]
        url = details.get("preview_url") or ""
    except Exception:
        url = ""
    _preview_url_cache[published_id] = url
    return url or None


def get_preview_bytes(m: dict) -> tuple[bytes, str] | None:
    """Return (image_bytes, content_type) for a map, or None."""
    local = m.get("preview")
    if local:
        try:
            data = Path(local).read_bytes()
            return data, _sniff_image(data)
        except OSError:
            pass

    wid = m.get("workshop_id")
    if not wid:
        return None
    cached = _cache_dir() / f"{wid}.img"
    if cached.is_file():
        try:
            data = cached.read_bytes()
            return data, _sniff_image(data)
        except OSError:
            pass
    url = _steam_preview_url(wid)
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = r.read()
        cached.write_bytes(data)
        return data, _sniff_image(data)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Map discovery
# --------------------------------------------------------------------------- #

def _read_vdf_title(vdf: Path) -> str | None:
    try:
        m = TITLE_RE.search(vdf.read_text(errors="ignore"))
        if m:
            return m.group(1).strip()
    except OSError:
        pass
    return None


def _find_map_file(folder: Path) -> Path | None:
    best: Path | None = None
    try:
        files = list(folder.glob("*.udk")) + list(folder.glob("*.upk"))
    except OSError:
        return None
    for p in files:
        try:
            if best is None or p.stat().st_size > best.stat().st_size:
                best = p
        except OSError:
            continue
    return best


def list_maps(cooked: Path) -> list[dict]:
    maps: list[dict] = []

    def add(name, path: Path, source: str, preview: Path | None, workshop_id: str | None = None):
        try:
            size = path.stat().st_size
        except OSError:
            return
        maps.append({
            "id": _map_id(str(path)),
            "name": name,
            "path": str(path),
            "size": size,
            "source": source,
            "preview": str(preview) if preview else None,
            "workshop_id": workshop_id,
        })

    sa = steamapps_from_cooked(cooked)
    if sa:
        ws = sa / "workshop/content/252950"
        if ws.is_dir():
            for item in sorted(ws.iterdir()):
                if not item.is_dir():
                    continue
                mf = _find_map_file(item)
                if not mf:
                    continue
                title = None
                for vdf in sorted(item.glob("*.vdf")):
                    title = _read_vdf_title(vdf)
                    if title:
                        break
                add(title or mf.stem, mf, "workshop", _find_local_preview(item), item.name)

    mods = cooked / "mods"
    if mods.is_dir():
        for item in sorted(mods.iterdir()):
            if item.is_dir():
                mf = _find_map_file(item)
                if mf:
                    add(item.name, mf, "mods", _find_local_preview(item))
            elif item.suffix.lower() in (".udk", ".upk"):
                add(item.stem, item, "mods", None)

    maps_dir = Path(os.environ.get("RL_MAPS", str(Path.home() / "maps")))
    if maps_dir.is_dir():
        for p in sorted(maps_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in (".udk", ".upk"):
                add(p.stem, p, "maps", None)
            elif p.is_dir():
                mf = _find_map_file(p)
                if mf:
                    add(p.name, mf, "maps", _find_local_preview(p))

    maps.sort(key=lambda m: (m["source"], m["name"].lower()))
    return maps


def resolve_map(cooked: Path, needle: str) -> dict | None:
    p = Path(needle)
    if p.is_file() and p.suffix.lower() in (".udk", ".upk"):
        return {"id": _map_id(str(p)), "name": p.stem, "path": str(p),
                "size": p.stat().st_size, "source": "path", "preview": None, "workshop_id": None}
    n = needle.lower()
    candidates = list_maps(cooked)
    for m in candidates:
        if n in (m["name"].lower(), Path(m["path"]).stem.lower(), Path(m["path"]).name.lower()):
            return m
    for m in candidates:
        if n in m["name"].lower() or n in Path(m["path"]).stem.lower():
            return m
    return None


def workshop_url(workshop_id: str | None) -> str | None:
    return (f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}"
            if workshop_id else None)


def find_map_by(cooked: Path, workshop_id=None, name=None, sha256=None) -> dict | None:
    """Locate a local map by Workshop id, then content hash, then name."""
    maps = list_maps(cooked)
    if workshop_id:
        for m in maps:
            if str(m.get("workshop_id") or "") == str(workshop_id):
                return m
    if sha256:
        for m in maps:
            if m.get("size") and file_hash(Path(m["path"])) == sha256:
                return m
    if name:
        return resolve_map(cooked, name)
    return None


# --------------------------------------------------------------------------- #
# Slot state / actions
# --------------------------------------------------------------------------- #

def _slot_paths(cooked: Path, slot: str) -> tuple[Path, Path, Path]:
    return (cooked / SLOTS[slot][0],
            cooked / (SLOTS[slot][0] + ".orig"),
            cooked / SLOTS[slot][1])


def _effective_backup(backup: Path, alt: Path) -> Path | None:
    if backup.is_file():
        return backup
    if alt.is_file():
        return alt
    return None


def slot_state(cooked: Path, slot: str = DEFAULT_SLOT) -> dict:
    slot_file, backup, alt = _slot_paths(cooked, slot)
    eff = _effective_backup(backup, alt)
    state = {
        "slot": slot, "slot_file": str(slot_file), "exists": slot_file.is_file(),
        "state": "missing", "map_name": None, "map_id": None, "size": 0, "modified": None,
        "backup_exists": bool(eff), "backup_path": str(eff) if eff else None,
        "sha256": None, "workshop_id": None,
    }
    if not slot_file.is_file():
        return state

    st = slot_file.stat()
    state["size"] = st.st_size
    state["modified"] = int(st.st_mtime)
    slot_hash = file_hash(slot_file)
    state["sha256"] = slot_hash

    if eff and file_hash(eff) == slot_hash:
        state["state"] = "stock"
        return state

    for m in list_maps(cooked):
        if m["size"] != state["size"]:
            continue
        if file_hash(Path(m["path"])) == slot_hash:
            state["state"] = "custom"
            state["map_name"] = m["name"]
            state["map_id"] = m["id"]
            state["workshop_id"] = m.get("workshop_id")
            return state

    state["state"] = "unknown"
    return state


def use_map(cooked: Path, slot: str, src: Path) -> dict:
    slot_file, backup, alt = _slot_paths(cooked, slot)
    if not src.is_file():
        raise RuntimeError(f"map file not found: {src}")
    if not slot_file.is_file():
        raise RuntimeError(f"slot file not found: {slot_file}")

    eff = _effective_backup(backup, alt)
    if eff is None:
        if slot_state(cooked, slot)["state"] == "custom":
            raise RuntimeError(
                "refusing to back up: the slot holds a custom map and no stock "
                "backup exists. Restore the stock map first (Steam: Verify "
                "integrity of game files)."
            )
        shutil.copy2(slot_file, backup)
        eff = backup

    tmp = slot_file.with_suffix(slot_file.suffix + ".new")
    shutil.copyfile(src, tmp)
    try:
        shutil.copystat(slot_file, tmp)
    except OSError:
        pass
    os.replace(tmp, slot_file)  # atomic on the same filesystem
    return {"ok": True, "action": "use", "map_name": src.stem,
            "slot_file": str(slot_file), "backup": str(eff), "size": slot_file.stat().st_size}


def restore_slot(cooked: Path, slot: str) -> dict:
    slot_file, backup, alt = _slot_paths(cooked, slot)
    eff = _effective_backup(backup, alt)
    if eff is None:
        raise RuntimeError("no stock backup found - nothing to restore")
    tmp = slot_file.with_suffix(slot_file.suffix + ".new")
    shutil.copyfile(eff, tmp)
    os.replace(tmp, slot_file)
    return {"ok": True, "action": "restore", "slot_file": str(slot_file),
            "restored_from": str(eff), "size": slot_file.stat().st_size}


# --------------------------------------------------------------------------- #
# Rocket Plugin staging (install / status / fetch)
# --------------------------------------------------------------------------- #

def install_map(cooked: Path, src: Path) -> list[str]:
    if not src.is_file():
        raise RuntimeError(f"no such file: {src}")
    mods = cooked / "mods"
    rp = cooked / "rocketplugin"
    mods.mkdir(parents=True, exist_ok=True)
    rp.mkdir(parents=True, exist_ok=True)
    a = mods / src.name
    b = rp / (src.stem + ".upk")
    shutil.copy2(src, a)
    shutil.copy2(src, b)
    return [str(a), str(b)]


def _http_get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=6) as r:
        return json.loads(r.read())


def host_status(host: str, port: int) -> dict:
    return _http_get_json(f"http://{host}:{port}/status")


def fetch_host_map(cooked: Path, host: str, port: int) -> tuple[str, list[str]]:
    status = host_status(host, port)
    name = status.get("CurrentMap") or ""
    if not name:
        raise RuntimeError("host did not report a current map (file server on?)")
    with urllib.request.urlopen(f"http://{host}:{port}/map", timeout=120) as r:
        data = r.read()
    tmp = Path(tempfile.mkdtemp()) / (Path(name).stem + ".upk")
    tmp.write_bytes(data)
    try:
        staged = install_map(cooked, tmp)
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    return name, staged


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
    """Return {'caps':..., 'state':...} if the peer speaks rpmap, else None."""
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
    return {"caps": caps, "state": state}


def _peer_row(p: dict, default_port: int, got: dict | None, local_hash: str) -> dict:
    pport = int(p.get("port") or default_port)
    row = {
        "peer_id": p.get("peer_id"), "name": p.get("name"), "ip": p.get("ip"),
        "online": bool(p.get("online")), "rpmap": False, "port": pport,
        "slot": None, "sha256": None, "in_sync": "unknown", "version": None,
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
                roster.append(_peer_row(futs[fut], port, got, local_hash))
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
                                    "error": "peer is on stock Underpass"})
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


# --------------------------------------------------------------------------- #
# Web UI
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>rpmap - custom map loader</title>
<style>
  :root{--bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--fg:#c9d1d9;--fg2:#8b949e;
        --accent:#238636;--accent2:#2ea043;--blue:#1f6feb;--warn:#d29922;--red:#da3633;--white:#f0f6fc}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif}
  header{padding:12px 22px;background:var(--bg2);border-bottom:1px solid #30363d;display:flex;gap:12px;align-items:center}
  h1{font-size:16px;margin:0;color:var(--white);font-weight:800;letter-spacing:.02em}
  .badge{padding:3px 10px;border-radius:999px;font-size:12px;font-weight:700;white-space:nowrap}
  .b-stock{background:#1f6feb33;color:#79c0ff;border:1px solid #1f6feb}
  .b-custom{background:#d2992233;color:#e3b341;border:1px solid var(--warn)}
  .b-unknown{background:#da363333;color:#ff7b72;border:1px solid var(--red)}
  .b-missing{background:#6e768133;color:#8b949e;border:1px solid #6e7681}
  main{padding:18px 22px;max-width:1240px;margin:0 auto}

  /* hero: what is currently loaded */
  .hero{display:flex;gap:18px;align-items:center;border-radius:14px;padding:16px 18px;margin-bottom:18px;
        background:linear-gradient(90deg,#1d2b3a,#161b22);border:1px solid #30465c}
  .hero.is-custom{background:linear-gradient(90deg,#3a2f14,#161b22);border-color:var(--warn)}
  .hero.is-unknown{background:linear-gradient(90deg,#3a1a1a,#161b22);border-color:var(--red)}
  .hero.is-missing{background:linear-gradient(90deg,#22262b,#161b22);border-color:#6e7681}
  .hero-thumb{width:210px;height:118px;flex:0 0 auto;border-radius:10px;overflow:hidden;position:relative;
              background:#0b0f14;border:1px solid #30363d}
  .hero-thumb img{width:100%;height:100%;object-fit:cover;display:block}
  .hero-thumb .ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--fg2);font-size:28px}
  .hero-info{min-width:0;flex:1}
  .hero-label{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--fg2)}
  .hero-name{font-size:30px;font-weight:800;color:var(--white);line-height:1.15;margin:2px 0 6px;
             overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .hero-sub{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .hero-actions{flex:0 0 auto;display:flex;flex-direction:column;gap:8px;align-items:stretch}
  .hint{margin-top:10px;color:var(--fg2);font-size:12.5px}

  .row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  .muted{color:var(--fg2);font-size:12.5px}
  .grow{flex:1}
  button{background:var(--bg3);color:var(--white);border:1px solid #30363d;border-radius:8px;
         padding:8px 14px;font-size:13.5px;font-weight:600;cursor:pointer}
  button:hover{background:#30363d}
  button.primary{background:var(--accent);border-color:var(--accent)}
  button.primary:hover{background:var(--accent2)}
  button.danger{background:transparent;border-color:var(--red);color:#ff7b72}
  button:disabled{opacity:.5;cursor:default}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
  .card{background:var(--bg2);border:1px solid #30363d;border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
  .card.is-active{border-color:var(--warn);box-shadow:0 0 0 1px var(--warn) inset}
  .thumb{position:relative;aspect-ratio:16/9;background:var(--bg3);overflow:hidden}
  .thumb img{width:100%;height:100%;object-fit:cover;display:block}
  .thumb .ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--fg2);font-size:22px}
  .card .body{padding:10px 12px;display:flex;flex-direction:column;gap:6px;flex:1}
  .card .name{color:var(--white);font-weight:700;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .card .meta{display:flex;justify-content:space-between;align-items:center;gap:8px}
  .src{font-size:11px;padding:2px 7px;border-radius:6px;background:var(--bg3);color:var(--fg2);text-transform:uppercase;letter-spacing:.04em}
  .search{background:var(--bg3);border:1px solid #30363d;border-radius:8px;color:var(--fg);padding:8px 12px;min-width:220px}
  .toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:var(--bg3);
          border:1px solid #30363d;border-radius:8px;padding:10px 16px;color:var(--white);opacity:0;
          transition:opacity .2s;pointer-events:none;z-index:9;max-width:80vw}
  .toast.show{opacity:1}
  .toast a{pointer-events:auto}
  code{background:var(--bg3);padding:1px 6px;border-radius:5px;color:#79c0ff}
  .panel{background:var(--bg2);border:1px solid #30363d;border-radius:10px;padding:14px 16px;margin-top:18px}
  table.peers{width:100%;border-collapse:collapse}
  table.peers th,table.peers td{text-align:left;padding:7px 8px;border-bottom:1px solid #21262d}
  table.peers th{color:var(--fg2);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em}
  table.peers tr:last-child td{border-bottom:0}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#6e7681;margin-right:6px}
  .dot.on{background:var(--accent2)}
  .dot.off{background:var(--red)}
  code.secret{font-size:14px;letter-spacing:.04em}
</style></head>
<body>
<header>
  <h1>rpmap</h1>
  <span class="muted" id="verLabel"></span>
  <span class="grow"></span>
  <span class="muted" id="pathLabel"></span>
</header>
<main>
  <section class="hero" id="hero">
    <div class="hero-thumb" id="heroThumb"><span class="ph">&#127918;</span></div>
    <div class="hero-info">
      <div class="hero-label">Underpass slot currently loads</div>
      <div class="hero-name" id="currentName">-</div>
      <div class="hero-sub">
        <span class="badge b-missing" id="slotBadge">loading...</span>
        <span class="muted" id="currentMeta"></span>
      </div>
      <div class="hint">In game: <b>Free Play &rarr; arena "Underpass"</b> &mdash; NOT "Tokyo Underpass".
        Restore the stock map before playing online (Rumble uses Underpass).</div>
    </div>
    <div class="hero-actions">
      <button id="restoreBtn" class="danger" onclick="restore()">Restore stock Underpass</button>
    </div>
  </section>

  <div class="row" style="margin-bottom:12px">
    <input class="search" id="search" placeholder="Filter maps..." oninput="render()">
    <span class="muted" id="count"></span>
    <span class="grow"></span>
    <button onclick="load_state()">Refresh</button>
  </div>

  <div class="grid" id="grid"></div>

  <div class="panel">
    <div class="row">
      <b>Players</b>
      <span class="grow"></span>
      <span class="muted" id="peersStatus">loading...</span>
      <button onclick="syncAll()" title="Push your loaded map to every reachable peer">Load mine on everyone</button>
      <button onclick="loadPeers()">Refresh</button>
    </div>
    <div class="row" style="margin-top:10px">
      <span class="muted">Session code</span>
      <code class="secret" id="mySecret">-</code>
      <button onclick="copySecret()">Copy</button>
      <span class="grow"></span>
      <input class="search" id="joinCode" placeholder="Paste a peer's session code...">
      <button onclick="joinSession()">Join</button>
    </div>
    <table class="peers" id="peerTable" style="margin-top:10px"></table>
    <div class="muted" id="peersEmpty" style="margin-top:6px;display:none">
      No peers discovered. Start tailscaled, run both machines on the tailnet with the
      same session code, and reload.
    </div>
  </div>
</main>
<div class="toast" id="toast"></div>
<script>
let STATE = null;
const token = new URLSearchParams(location.search).get('token') || '';
const api = (p, opts={}) => fetch(p + (p.includes('?')?'&':'?') + 'token=' + encodeURIComponent(token), opts);
const prevUrl = id => '/api/preview?id=' + encodeURIComponent(id) + '&token=' + encodeURIComponent(token);

function fmtSize(n){ if(!n) return ''; const u=['B','KB','MB','GB']; let i=0; while(n>=1024&&i<u.length-1){n/=1024;i++;} return n.toFixed(i?1:0)+' '+u[i]; }
function toast(msg, ms=2600){ const t=document.getElementById('toast'); t.textContent=msg; t.classList.add('show'); setTimeout(()=>t.classList.remove('show'), ms); }
function toastLink(msg, url, ms=6000){
  const t = document.getElementById('toast');
  t.textContent = '';
  t.appendChild(document.createTextNode(msg + (url ? '  ' : '')));
  if(url){
    const a = document.createElement('a');
    a.href = url; a.target = '_blank'; a.rel = 'noopener';
    a.textContent = 'Open Workshop \u2197';
    a.style.color = '#79c0ff'; a.style.textDecoration = 'underline';
    t.appendChild(a);
  }
  t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), ms);
}
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

async function load_state(){
  try { STATE = await (await api('/api/state')).json(); }
  catch(e){ toast('Cannot reach the local server'); return; }
  render();
}

function render(){
  if(!STATE) return;
  const s = STATE.slot;
  document.getElementById('pathLabel').textContent = STATE.cooked || '';

  const hero = document.getElementById('hero');
  hero.className = 'hero is-' + s.state;
  const name = s.map_name ? s.map_name
             : (s.state==='stock' ? 'Stock Underpass'
             : (s.state==='missing' ? 'Slot file missing' : 'Unknown map'));
  document.getElementById('currentName').textContent = name;

  const labels = {stock:'stock Underpass', custom:'custom map', unknown:'unrecognised', missing:'no slot file'};
  const badge = document.getElementById('slotBadge');
  badge.className = 'badge b-' + ({stock:'stock',custom:'custom',unknown:'unknown',missing:'missing'}[s.state]||'unknown');
  badge.textContent = labels[s.state] || s.state;

  const bits = [];
  if (s.state==='custom') bits.push('swap active');
  if (s.size) bits.push(fmtSize(s.size));
  if (s.modified) bits.push('changed ' + new Date(s.modified*1000).toLocaleString());
  bits.push(s.backup_exists ? 'backup ok' : 'no backup');
  document.getElementById('currentMeta').textContent = bits.join('  \u2022  ');
  document.getElementById('restoreBtn').disabled = !s.backup_exists || s.state==='stock';

  const ht = document.getElementById('heroThumb');
  if (s.map_id && s.state==='custom') {
    ht.innerHTML = `<img src="${prevUrl(s.map_id)}" onerror="this.remove()"><span class="ph">&#127918;</span>`;
  } else {
    ht.innerHTML = `<span class="ph">${s.state==='stock' ? '\u{1F3DF}\uFE0F' : '\u2753'}</span>`;
  }

  const q = (document.getElementById('search').value||'').toLowerCase();
  const maps = (STATE.maps||[]).filter(m => !q || m.name.toLowerCase().includes(q));
  document.getElementById('count').textContent = maps.length + ' map(s)';
  document.getElementById('grid').innerHTML = maps.map(m => {
    const active = s.map_id && s.map_id === m.id;
    return `<div class="card${active?' is-active':''}">
      <div class="thumb">
        <span class="ph">&#127918;</span>
        <img loading="lazy" src="${prevUrl(m.id)}" onerror="this.remove()">
      </div>
      <div class="body">
        <div class="name" title="${esc(m.name)}">${esc(m.name)}</div>
        <div class="meta"><span class="src">${esc(m.source)}</span><span class="muted">${fmtSize(m.size)}</span></div>
        <button class="primary" onclick="useMap('${m.id}')" ${active?'disabled':''}>${active?'Loaded':'Load into slot'}</button>
      </div></div>`;
  }).join('') || '<div class="muted">No maps found. Put .udk/.upk files in ~/maps, or subscribe on Steam Workshop.</div>';
}

async function useMap(id){
  const m = (STATE.maps||[]).find(x=>x.id===id); if(!m) return;
  if(!confirm(`Load "${m.name}" into the Underpass slot?\n\nThen in game: Free Play -> arena "Underpass".`)) return;
  toast('Swapping map...');
  const j = await (await api('/api/use',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})})).json();
  toast(j.ok ? ('Loaded: ' + m.name) : ('Error: ' + (j.error||'failed')));
  load_state();
}
async function restore(){
  if(!confirm('Restore the stock Underpass? Do this before playing online.')) return;
  const j = await (await api('/api/restore',{method:'POST'})).json();
  toast(j.ok ? 'Stock Underpass restored' : ('Error: ' + (j.error||'failed')));
  load_state();
}

let SESSION = null;
async function loadSession(){
  try{ SESSION = await (await api('/api/session')).json(); }
  catch(e){ SESSION = null; return; }
  const el = document.getElementById('mySecret');
  if(el) el.textContent = SESSION.secret || '-';
  const vl = document.getElementById('verLabel');
  if(vl) vl.textContent = 'v' + (SESSION.version || '?');
}
async function copySecret(){
  if(!SESSION || !SESSION.secret) return;
  try{ await navigator.clipboard.writeText(SESSION.secret); toast('Session code copied'); }
  catch(e){ toast(SESSION.secret); }
}
async function joinSession(){
  const v = (document.getElementById('joinCode').value||'').trim();
  if(!v) return;
  const r = await (await api('/api/session',{method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({secret:v})})).json();
  if(r.ok){ toast('Session code saved'); document.getElementById('joinCode').value=''; loadSession(); loadPeers(); }
  else toast('Error: ' + (r.error||'failed'));
}
function syncBadge(s){
  if(s==='same') return '<span class="badge b-stock">in sync</span>';
  if(s==='different') return '<span class="badge b-custom">out of sync</span>';
  return '<span class="badge b-missing">unknown</span>';
}
function peerRow(p, isSelf){
  const dot = `<span class="dot ${p.online?'on':'off'}"></span>`;
  const slot = p.slot || {};
  const map = slot.map_name ? esc(slot.map_name)
            : slot.state==='stock' ? 'Stock Underpass'
            : slot.state ? esc(slot.state) : '\u2014';
  const size = slot.size ? fmtSize(slot.size) : '';
  const hash = p.sha256 ? `<code>${esc(String(p.sha256).slice(0,10))}</code>` : '';
  let verCell = '';
  if(p.version){
    if(!isSelf && SELF_VER && p.version !== SELF_VER){
      verCell = `<span style="color:var(--warn)" title="Peer runs v${esc(p.version)}; you run v${esc(SELF_VER)}">v${esc(p.version)} \u26a0</span>`;
    } else {
      verCell = `<span class="muted">v${esc(p.version)}</span>`;
    }
  }
  let actions = isSelf ? '' : syncBadge(p.in_sync);
  if(!isSelf && p.rpmap){
    const id = esc(p.peer_id||'');
    actions += ` <button onclick="syncFrom('${id}')" title="Load their map here">Switch me</button>`
             + ` <button onclick="syncTo('${id}')" title="Load your map there">Set theirs</button>`;
  }
  return `<tr>
    <td>${dot}${esc(p.name||'?')}${isSelf?' <span class="muted">(you)</span>':''}</td>
    <td class="muted">${esc(p.ip||'')}</td>
    <td>${verCell}</td>
    <td>${map}</td>
    <td class="muted">${size}</td>
    <td>${hash}</td>
    <td style="text-align:right;white-space:nowrap">${actions}</td>
  </tr>`;
}
let PEERS = null;
let SELF_VER = '';
async function loadPeers(){
  let P;
  try{ P = await (await api('/api/peers')).json(); }
  catch(e){ document.getElementById('peersStatus').textContent = 'server unreachable'; return; }
  PEERS = P.peers || [];
  const self = P.self || {};
  SELF_VER = self.version || '';
  const rows = [ peerRow({name:self.name, ip:self.ip, online:true, slot:self.slot,
                          sha256:self.sha256, version:self.version, in_sync:'same'}, true) ];
  PEERS.forEach(p => rows.push(peerRow(p, false)));
  document.getElementById('peerTable').innerHTML =
    '<tr><th>Player</th><th>IP</th><th>Version</th><th>Loaded map</th><th>Size</th><th>Hash</th><th></th></tr>'
    + rows.join('');
  const n = PEERS.length;
  const same = PEERS.filter(p => p.in_sync==='same').length;
  const vdiff = PEERS.filter(p => p.version && SELF_VER && p.version !== SELF_VER).length;
  document.getElementById('peersEmpty').style.display = n ? 'none' : 'block';
  let status = n ? (same + '/' + n + ' in sync') : 'no peers';
  if(vdiff) status += ' \u00b7 ' + vdiff + ' on a different rpmap version';
  document.getElementById('peersStatus').textContent = status;
}
function peerTarget(p){ return {ip:p.ip, port:p.port, name:p.name}; }
function findPeer(id){ return (PEERS||[]).find(x => x.peer_id===id); }
async function doSync(mode, targets){
  if(!targets.length){ toast('No reachable peer running rpmap'); return; }
  toast(mode==='push' ? 'Switching peers...' : 'Switching this machine...');
  let r;
  try{
    r = await (await api('/api/sync',{method:'POST',headers:{'Content-Type':'application/json'},
             body:JSON.stringify({mode, peers:targets})})).json();
  }catch(e){ toast('Sync failed'); return; }
  const res = r.results || [];
  const bad = res.filter(x => !x.ok);
  if(bad.length){
    const b = bad[0];
    const who = b.peer || 'peer';
    let msg;
    if(b.error === 'not installed'){
      msg = (mode === 'pull')
        ? "You don't have this map (get it from the Workshop first)"
        : who + " doesn't have the map (needs a Workshop download)";
    } else if(b.error === 'version mismatch'){
      msg = 'Map content differs on ' + who + ' \u2014 not swapped';
    } else if(b.error === 'peer is on stock Underpass'){
      msg = who + ' is on Stock Underpass';
    } else {
      msg = (b.peer ? who + ': ' : '') + (b.error || 'sync failed');
    }
    toastLink(msg, b.url);
  } else {
    toast(mode==='push' ? ('Pushed to ' + res.length + ' peer(s)')
                        : ('Switched to ' + ((res[0]&&res[0].map_name)||'their map')));
  }
  load_state(); loadPeers();
}
function syncFrom(id){ const p=findPeer(id); if(p) doSync('pull', [peerTarget(p)]); }
function syncTo(id){ const p=findPeer(id); if(p) doSync('push', [peerTarget(p)]); }
function syncAll(){ doSync('push', (PEERS||[]).filter(p=>p.rpmap).map(peerTarget)); }

load_state(); loadSession(); loadPeers();
setInterval(load_state, 4000);
setInterval(loadPeers, 8000);
</script>
</body></html>
"""


def make_handler(engine: dict):
    cooked: Path = engine["cooked"]
    slot: str = engine["slot"]
    token: str = engine["token"]
    host: str = engine["host"]
    ident: dict = engine.get("ident") or {}
    port: int = int(engine.get("port") or DEFAULT_PORT)
    dev: bool = bool(engine.get("dev"))

    allowed_hosts = {"127.0.0.1", "localhost", "::1", host}
    for _ip in (ident.get("ips") or []):
        allowed_hosts.add(_ip)
    if ident.get("dns"):
        allowed_hosts.add(ident["dns"])

    class Handler(BaseHTTPRequestHandler):
        server_version = "rpmap"

        def log_message(self, *a):
            pass

        def _send(self, body: bytes, ctype: str, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(json.dumps(obj).encode(), "application/json", code)

        def _authed(self, q) -> bool:
            if q.get("token", [""])[0] == token:
                return True
            secret = engine.get("secret") or ""
            auth = self.headers.get("Authorization") or ""
            if secret and auth.startswith("Bearer "):
                return hmac.compare_digest(auth[7:].strip(), secret)
            return False

        def _source_ok(self) -> bool:
            try:
                addr = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return False
            if addr.is_loopback:
                return True
            if dev and addr.is_private:
                return True
            return addr in TAILNET

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").split(":")[0] in allowed_hosts

        def do_GET(self):
            if not self._source_ok():
                return self._json({"error": "forbidden source"}, 403)
            if not self._host_ok():
                return self._json({"error": "bad host"}, 403)
            u = urlparse(self.path)
            q = parse_qs(u.query)

            if u.path == "/":
                return self._send(PAGE.encode(), "text/html; charset=utf-8")

            if not u.path.startswith("/api/"):
                return self._json({"error": "not found"}, 404)
            if not self._authed(q):
                return self._json({"error": "bad token"}, 403)

            if u.path == "/api/state":
                return self._json({"cooked": str(cooked), "maps": list_maps(cooked),
                                   "slot": slot_state(cooked, slot),
                                   "peer": {"peer_id": ident.get("peer_id"),
                                            "name": ident.get("name")}})

            if u.path == "/api/capabilities":
                st = slot_state(cooked, slot)
                return self._json({"product": PRODUCT, "version": APP_VERSION,
                                   "peer_id": ident.get("peer_id"), "name": ident.get("name"),
                                   "port": port,
                                   "slot": {"slot": st["slot"], "state": st["state"],
                                            "map_name": st["map_name"], "size": st["size"],
                                            "sha256": st.get("sha256") or None,
                                            "workshop_id": st.get("workshop_id")}})

            if u.path == "/api/session":
                return self._json({"peer_id": ident.get("peer_id"), "name": ident.get("name"),
                                   "port": port, "version": APP_VERSION,
                                   "secret": engine.get("secret") or ""})

            if u.path == "/api/preview":
                mid = q.get("id", [""])[0]
                m = next((x for x in list_maps(cooked) if x["id"] == mid), None)
                if m:
                    got = get_preview_bytes(m)
                    if got:
                        return self._send(got[0], got[1])
                return self._json({"error": "no preview"}, 404)

            if u.path == "/api/peers":
                return self._json(gather_peers(cooked, slot, engine.get("cfg") or {}, ident))

            return self._json({"error": "not found"}, 404)

        def do_POST(self):
            if not self._source_ok():
                return self._json({"error": "forbidden source"}, 403)
            if not self._host_ok():
                return self._json({"error": "bad host"}, 403)
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._authed(q):
                return self._json({"error": "bad token"}, 403)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                payload = {}
            try:
                if u.path == "/api/use":
                    mp = next((m for m in list_maps(cooked) if m["id"] == payload.get("id")), None)
                    if not mp:
                        return self._json({"error": "unknown map id"}, 400)
                    return self._json(use_map(cooked, slot, Path(mp["path"])))
                if u.path == "/api/restore":
                    return self._json(restore_slot(cooked, slot))
                if u.path == "/api/session":
                    new = str(payload.get("secret") or "").strip() or new_secret()
                    engine["secret"] = new
                    engine.setdefault("cfg", {})["session_secret"] = new
                    save_config(engine["cfg"])
                    return self._json({"ok": True, "secret": new})
                if u.path == "/api/peer/switch":
                    return self._json(peer_switch(cooked, slot, payload))
                if u.path == "/api/sync":
                    return self._json(sync_action(cooked, slot, engine.get("cfg") or {},
                                                  str(payload.get("mode") or "push"),
                                                  payload.get("peers") or []))
            except Exception as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"error": "not found"}, 404)

    return Handler


def _find_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return preferred


def run_server(cooked: Path, slot: str, host: str, port: int, open_browser: bool,
               secret: str | None = None, dev: bool = False) -> int:
    cfg = load_config()
    if secret:
        cfg["session_secret"] = secret
    if not cfg.get("session_secret"):
        cfg["session_secret"] = new_secret()
    if not cfg.get("port"):
        cfg["port"] = port
    save_config(cfg)
    ident = local_identity(cfg)
    token = secrets.token_urlsafe(16)
    actual = _find_port(port) if host == "127.0.0.1" else port
    httpd = ThreadingHTTPServer((host, actual),
                                make_handler({"cooked": cooked, "slot": slot,
                                              "token": token, "host": host,
                                              "secret": cfg["session_secret"],
                                              "cfg": cfg, "ident": ident,
                                              "dev": dev, "port": actual}))
    url = f"http://127.0.0.1:{actual}/?token={token}"
    print(f"rpmap UI  ->  {url}", flush=True)
    print(f"slot: {SLOTS[slot][0]}   cooked: {cooked}", flush=True)
    print(f"peer: {ident['name']} ({ident['peer_id']})", flush=True)
    print(f"session: {cfg['session_secret']}   <- share with peers / paste in the UI", flush=True)
    if host == "127.0.0.1":
        print("hint: for peer sync run:  serve --host <tailscale-ip>", flush=True)
    else:
        print(f"note: bound to {host}; peers reach this on port {actual}", flush=True)
    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
    return 0


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
        return "Stock Underpass"
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
                          dev=getattr(args, "dev", False))

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
        print(f"Loaded '{m['name']}' into the {args.slot} slot.")
        print('In game: Free Play -> arena "Underpass" (not "Tokyo Underpass").')
        return 0

    if args.cmd == "restore":
        restore_slot(cooked, args.slot)
        print("Stock Underpass restored.")
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
