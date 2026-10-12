"""Configuration, identity and TLS bootstrap for rpmap.

Constants and helpers shared by every other rpmap module: the slot tables, the
on-disk config file, the app version and the system CA-bundle fallback.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

# slot key -> (file RL loads, alternate "original" backup name other tools use)
SLOTS = {
    "utopia": ("Labs_Utopia_P.upk", "ORIGINAL_UTOPIA.upk"),
    "underpass": ("Labs_Underpass_P.upk", "ORIGINAL_UNDERPASS.upk"),
}
# In-game arena name per slot, plus a selection gotcha shown in the UI.
SLOT_INFO = {
    "utopia": {"arena": "Utopia Retro", "note": ""},
    "underpass": {"arena": "Underpass", "note": 'NOT "Tokyo Underpass"'},
}
DEFAULT_SLOT = "utopia"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".jfif", ".webp"}
TITLE_RE = re.compile(r'"title"\s+"([^"]*)"')
PATH_RE = re.compile(r'"path"\s+"([^"]*)"')


# --------------------------------------------------------------------------- #
# Config file / identity
# --------------------------------------------------------------------------- #

PRODUCT = "rpmap"
DEFAULT_PORT = 8787
TAILNET = ipaddress.ip_network("100.64.0.0/10")

# Where the packaged builds live (used only by the self-updater).
GITHUB_REPO = "JThFr/rocket-league-workshop-map-swap"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"


def _read_app_version() -> str:
    """The version, from the repo's ``VERSION`` file (the single source of truth).

    Packaged builds bundle ``VERSION`` next to the executable (``sys._MEIPASS``);
    running from source reads it from the repo root. Falls back to ``0.0.0`` -
    which disables the self-updater - if the file can't be found.
    """
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    roots.append(Path(__file__).resolve().parents[1])
    for root in roots:
        try:
            text = (root / "VERSION").read_text().strip()
        except OSError:
            continue
        if text:
            return text
    return "0.0.0"


APP_VERSION = _read_app_version()


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
# TLS trust store (self-update)
# --------------------------------------------------------------------------- #

# System CA bundles to fall back on (POSIX), in preference order.
CA_BUNDLE_CANDIDATES = (
    "/etc/ssl/certs/ca-certificates.crt",                 # Debian/Ubuntu, Fedora symlink
    "/etc/pki/tls/certs/ca-bundle.crt",                   # RHEL/Fedora (traditional)
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # RHEL/Fedora (ca-trust)
    "/etc/ssl/cert.pem",                                  # Alpine
    "/usr/local/etc/openssl/cert.pem",                    # macOS (Homebrew)
)


def ensure_ca_bundle() -> str | None:
    """Point Python's TLS at the system CA bundle if it can't find one itself.

    A packaged build ships its own OpenSSL, whose compiled-in default CA path
    usually does not exist inside the bundle (seen on Fedora/Bazzite). Every
    HTTPS request then fails certificate verification, and because the update
    check treats *any* error as "no update", self-update silently never fires.
    ``SSL_CERT_FILE`` is read when the default TLS context is created, so
    setting it early fixes both the release check and the download.
    """
    if os.name == "nt" or os.environ.get("SSL_CERT_FILE"):
        return None                       # Windows uses its own trust store
    for path in CA_BUNDLE_CANDIDATES:
        if os.path.isfile(path):
            os.environ["SSL_CERT_FILE"] = path
            return path
    return None


ensure_ca_bundle()
