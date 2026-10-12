"""Self-update from GitHub releases for packaged rpmap builds."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from rpmap_config import APP_VERSION, GITHUB_RELEASES_API, GITHUB_RELEASES_PAGE, PRODUCT


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

    ``kind`` is ``"exe"`` (Windows), ``"appimage"`` (a FUSE mount point, so it
    must be swapped after this process exits) or ``"file"`` (a plain Linux
    binary, which can be replaced in place).
    """
    if os.name == "nt":
        return (Path(sys.executable), "exe") if _is_frozen() else None
    appimage = os.environ.get("APPIMAGE")
    if appimage and Path(appimage).is_file():
        return (Path(appimage), "appimage")
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
    if _version_tuple(APP_VERSION) == (0, 0, 0):
        return None                       # unknown version (VERSION file missing)
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


def _windows_swap_script(new_exe: Path, current_exe: Path) -> str:
    """The .cmd that swaps the exe after we exit and relaunches it.

    ``%*`` forwards this script's own arguments, so the relaunched build keeps
    the flags we were started with (``--slot``, ``--cooked``, ``--restore-on-exit``,
    ``--host`` …). The caller passes ``sys.argv[1:]`` when invoking the script.
    """
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
        f'start "" "{current_exe}" %*',
        'del "%~f0"',
        "",
    ]
    return "\r\n".join(lines)


def _spawn_windows_swap(new_exe: Path, current_exe: Path, args: list[str]) -> None:
    """Write (and detach) the .cmd that swaps the exe and relaunches it.

    A running .exe can't be overwritten on Windows, so retry ``move`` until the
    file unlocks, then start the new build. ``ping`` is the delay because
    ``timeout`` needs a console, which a detached process doesn't have.
    """
    cmd = Path(tempfile.gettempdir()) / "rpmap-update.cmd"
    cmd.write_text(_windows_swap_script(new_exe, current_exe))
    detached = 0x00000008          # DETACHED_PROCESS
    new_group = 0x00000200         # CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(["cmd", "/c", str(cmd), *args],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True,
                     creationflags=detached | new_group)


def _posix_swap_script(new_file: Path, target: Path, args: list[str]) -> str:
    """The shell script that swaps the AppImage/ELF after we exit, then relaunches.

    The relaunch includes ``args`` (our ``sys.argv[1:]``), shell-quoted, so the
    new build keeps the flags we were started with.
    """
    relaunch = " ".join(shlex.quote(a) for a in [str(target), *args])
    lines = [
        "#!/bin/sh",
        "n=0",
        f'while ! mv -f "{new_file}" "{target}" 2>/dev/null; do',
        "  n=$((n+1))",
        "  [ $n -ge 120 ] && exit 1",
        "  sleep 1",
        "done",
        f'chmod +x "{target}"',
        f'nohup {relaunch} >/dev/null 2>&1 &',
        'rm -f "$0"',
        "",
    ]
    return "\n".join(lines)


def _spawn_posix_swap(new_file: Path, target: Path, args: list[str]) -> None:
    """Write (and detach) the shell script that swaps the file and relaunches.

    A running AppImage is a FUSE mount point, so ``mv`` over it fails until the
    process exits and unmounts - hence the retry loop.
    """
    script = Path(tempfile.gettempdir()) / "rpmap-update.sh"
    script.write_text(_posix_swap_script(new_file, target, args))
    os.chmod(script, 0o755)
    subprocess.Popen(["/bin/sh", str(script)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


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
    args = list(sys.argv[1:])       # keep our flags across the restart
    if kind == "exe":
        _spawn_windows_swap(staged, path, args)
        print(f"rpmap: updating to {info.get('version')} - restarting...", flush=True)
        os._exit(0)
    if kind == "appimage":
        _spawn_posix_swap(staged, path, args)
        print(f"rpmap: updating to {info.get('version')} - restarting...", flush=True)
        os._exit(0)
    try:                       # plain Linux binary: replace the running file in place
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
