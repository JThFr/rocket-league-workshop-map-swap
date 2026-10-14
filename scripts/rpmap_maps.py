"""Map discovery, hashing, previews and slot swapping for rpmap."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from rpmap_config import DEFAULT_SLOT, IMAGE_EXTS, PATH_RE, SLOTS, SLOT_INFO, TITLE_RE

_hash_cache: dict[tuple[str, int, int], str] = {}
_preview_url_cache: dict[str, str] = {}


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


def _epic_is_rocket_league(entry: dict) -> bool:
    """Does an Epic manifest/install record describe Rocket League?"""
    if str(entry.get("MandatoryAppFolderName") or "").strip().lower() == "rocketleague":
        return True
    if "rocket league" in str(entry.get("DisplayName") or "").lower():
        return True
    # "Sugar" is Rocket League's Epic catalogue codename.
    return str(entry.get("AppName") or "").strip().lower() == "sugar"


def _epic_installs() -> list[Path]:
    """Rocket League install roots recorded by the Epic Games Launcher.

    Two JSON sources under ``%ProgramData%\\Epic``, both carrying an absolute
    ``InstallLocation`` (so a game on another drive is still found): the
    per-game ``*.item`` manifests, and the launcher's ``LauncherInstalled.dat``.
    Best effort and Windows-only in practice - an unreadable/absent file is just
    skipped. Steam installs never go through here.
    """
    roots: list[Path] = []

    def _add(location) -> None:
        if not location:
            return
        p = Path(str(location))
        if p not in roots:
            roots.append(p)

    programdata = Path(os.environ.get("ProgramData") or "C:/ProgramData")

    manifests = programdata / "Epic/EpicGamesLauncher/Data/Manifests"
    if manifests.is_dir():
        for item in manifests.glob("*.item"):
            try:
                data = json.loads(item.read_text(errors="ignore"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and _epic_is_rocket_league(data):
                _add(data.get("InstallLocation"))

    installed = programdata / "Epic/UnrealEngineLauncher/LauncherInstalled.dat"
    try:
        data = json.loads(installed.read_text(errors="ignore"))
        for entry in (data.get("InstallationList") or []):
            if isinstance(entry, dict) and _epic_is_rocket_league(entry):
                _add(entry.get("InstallLocation"))
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    return roots


# Last-resort locations, checked after the Steam library scan and the Epic
# manifests: Steam first (this project's primary store), Epic after. Strings so
# a test can neutralise them (a developer's machine may have a real install).
_COOKED_FALLBACKS = (
    "/var/mnt/storage/SteamLibrary/steamapps/common/rocketleague/TAGame/CookedPCConsole",
    "~/.steam/steam/steamapps/common/rocketleague/TAGame/CookedPCConsole",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common/rocketleague/TAGame/CookedPCConsole",
    "C:/Program Files/Epic Games/rocketleague/TAGame/CookedPCConsole",
    "C:/Program Files (x86)/Epic Games/rocketleague/TAGame/CookedPCConsole",
)


def detect_cooked(override: str | None = None) -> Path | None:
    if override:
        p = Path(override)
        return p if p.is_dir() else None
    env = os.environ.get("RL_COOKED")
    if env and Path(env).is_dir():
        return Path(env)

    # Steam first (unchanged), then Epic Games Store.
    candidates: list[Path] = [Path(p).expanduser() for p in _COOKED_FALLBACKS]
    for lib in _steam_libraries():
        candidates.append(lib / "steamapps/common/rocketleague/TAGame/CookedPCConsole")
    for root in _epic_installs():
        candidates.append(root / "TAGame/CookedPCConsole")
    for c in candidates:
        if c.is_dir():
            return c
    return None


def steamapps_from_cooked(cooked: Path) -> Path | None:
    """The ``steamapps`` dir, only for a real Steam install (else None).

    A Steam install ends in ``…/steamapps/common/rocketleague/TAGame/CookedPCConsole``.
    An Epic Games Store install has the same ``rocketleague/TAGame`` tail but no
    ``steamapps``, so it must not be taken for one - the Steam Workshop content
    that :func:`list_maps` scans lives under ``steamapps``.
    """
    try:
        sa = cooked.parents[3]
    except IndexError:
        return None
    return sa if sa.name == "steamapps" and sa.is_dir() else None


# --------------------------------------------------------------------------- #
# Hashing
# --------------------------------------------------------------------------- #

def file_hash(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    # st_mtime_ns (not whole seconds) keeps the cache key distinct across rapid
    # swaps to a same-size map; a swap must never surface a stale hash.
    key = (str(path), st.st_size, st.st_mtime_ns)
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


def _workshop_dirs(cooked: Path) -> list[Path]:
    """Steam Workshop content folders for Rocket League (appid 252950).

    Normally the cooked install's own Steam library, but every Steam library we
    can find is also scanned: an Epic-cooked install (which has no ``steamapps``
    of its own) or content on a second drive still sees the maps the player
    subscribed to on Steam. Deduped, in a stable order.
    """
    out: list[Path] = []
    seen: set[str] = set()
    steamapps: list[Path] = []
    sa = steamapps_from_cooked(cooked)
    if sa:
        steamapps.append(sa)
    steamapps += [lib / "steamapps" for lib in _steam_libraries()]
    for base in steamapps:
        ws = base / "workshop/content/252950"
        try:
            key = str(ws.resolve())
        except OSError:
            key = str(ws)
        if key in seen or not ws.is_dir():
            continue
        seen.add(key)
        out.append(ws)
    return out


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

    seen_workshop: set[str] = set()
    for ws in _workshop_dirs(cooked):
        for item in sorted(ws.iterdir()):
            if not item.is_dir() or item.name in seen_workshop:
                continue
            mf = _find_map_file(item)
            if not mf:
                continue
            seen_workshop.add(item.name)
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


def map_key(m: dict) -> str:
    """A stable identity for a map, shared by the local grid and peer inventories.

    Workshop maps use their published file id; anything else falls back to
    lowercased name + size. Deliberately cheap - no hashing of multi-GB files -
    which is good enough for the "everyone already has it" filter.
    """
    wid = str(m.get("workshop_id") or "")
    if wid:
        return f"w:{wid}"
    return f"n:{(m.get('name') or '').lower()}:{m.get('size') or 0}"


def map_inventory(cooked: Path) -> list[dict]:
    """A compact list of the maps this machine has, for peers to intersect."""
    return [{"key": map_key(m), "name": m["name"], "size": m["size"],
             "workshop_id": m.get("workshop_id")} for m in list_maps(cooked)]


# --------------------------------------------------------------------------- #
# Slot state / actions
# --------------------------------------------------------------------------- #

def _slot_paths(cooked: Path, slot: str) -> tuple[Path, Path, Path]:
    return (cooked / SLOTS[slot][0],
            cooked / (SLOTS[slot][0] + ".orig"),
            cooked / SLOTS[slot][1])


# A cooked UE3 package starts with this tag, then uint16 version, uint16
# licensee version. Rocket League's own content is cooked with a nonzero
# licensee version (34, or 32 for a few helper packages); community maps built
# with the RL SDK/UDK are cooked with licensee version 0. That difference lets
# us tell a stock slot file from a swapped-in map even before a stock backup
# has been taken.
UE3_PACKAGE_TAG = 0x9E2A83C1


def _package_licensee_version(path: Path) -> int | None:
    """Licensee version from a package header, or None if this isn't a package."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return None
    if len(head) < 8:
        return None
    tag, _version, licensee = struct.unpack("<IHH", head)
    return licensee if tag == UE3_PACKAGE_TAG else None


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
        "slot": slot, "arena": SLOT_INFO.get(slot, {}).get("arena", slot),
        "note": SLOT_INFO.get(slot, {}).get("note", ""),
        "slot_file": str(slot_file), "exists": slot_file.is_file(),
        "state": "missing", "map_name": None, "map_id": None, "size": 0, "modified": None,
        "backup_exists": bool(eff), "backup_path": str(eff) if eff else None,
        "sha256": None, "workshop_id": None, "inferred": False,
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

    # No matching backup and no known map file. Read the package header: the
    # game's own files carry a nonzero licensee version, community maps 0.
    licensee = _package_licensee_version(slot_file)
    if licensee is not None:
        state["state"] = "stock" if licensee != 0 else "custom"
        state["inferred"] = True
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
                "integrity of game files; Epic: Verify in the launcher)."
            )
        shutil.copy2(slot_file, backup)
        eff = backup

    tmp = slot_file.with_suffix(slot_file.suffix + ".new")
    shutil.copyfile(src, tmp)
    try:
        # Preserve the slot file's permissions, but deliberately NOT its mtime:
        # keeping the old timestamp (shutil.copystat) let a same-size map reuse
        # the previous file's cache entry and report a stale sha256.
        shutil.copymode(slot_file, tmp)
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
