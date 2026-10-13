"""A bounded in-memory log ring, fed by teeing stdout/stderr.

The packaged app runs *detached* (a self-update relaunches it with no terminal,
and a windowed launch has no console), so every ``print`` and traceback goes to
a stream nobody reads - a failed update or a relay error is invisible. This
module tees ``sys.stdout``/``sys.stderr`` into a small in-memory buffer that the
web UI serves from ``/api/log`` and shows in its **Logs** panel.

It is deliberately best-effort and inert until :func:`install` is called, so
one-shot CLI commands keep their exact output and behaviour. Standard library
only; no imports from the rest of rpmap, so it can sit at the bottom of the
layering with ``rpmap_config``.
"""

from __future__ import annotations

import collections
import itertools
import sys
import threading
import time

MAXLEN = 500                 # entries kept (roughly the last few hundred lines)

_lock = threading.Lock()
_entries: collections.deque[dict] = collections.deque(maxlen=MAXLEN)
_seq = itertools.count(1)
_installed = False
_secrets: list[str] = []


def set_secrets(secrets) -> None:
    """Register strings (UI token, session code) that must not appear in logs."""
    _secrets[:] = [str(s) for s in secrets if s]


def _redact(text: str) -> str:
    for secret in _secrets:
        text = text.replace(secret, "\u2026redacted")
    return text


def add(msg: str, level: str = "info") -> None:
    """Append one line to the ring (a single trailing newline is dropped)."""
    text = _redact(str(msg).rstrip("\n"))
    if not text:
        return
    with _lock:
        _entries.append({"seq": next(_seq), "t": time.time(),
                         "level": level, "msg": text})


def entries(after: int = 0) -> tuple[list[dict], int]:
    """Entries with ``seq > after``, plus the newest seq (0 when the ring is empty)."""
    with _lock:
        got = [dict(e) for e in _entries if e["seq"] > after]
        newest = _entries[-1]["seq"] if _entries else 0
    return got, newest


class _Tee:
    """A text stream that writes through to the real one *and* into the ring.

    Only complete lines are recorded (a partial write is buffered until its
    newline), so multi-``print`` progress output stays one entry per line.
    ``level`` tags the line's origin - ``stderr`` is treated as an error.
    """

    def __init__(self, original, level: str):
        self._original = original
        self._level = level
        self._buf = ""

    def write(self, text):
        if self._original is not None:
            try:
                self._original.write(text)
            except Exception:
                pass
        if isinstance(text, str):
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                add(line, self._level)
        return len(text)

    def flush(self):
        if self._original is not None:
            try:
                self._original.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False

    def __getattr__(self, name):        # .encoding, .buffer, .name, ... -> delegate
        return getattr(self._original, name)


def install() -> None:
    """Tee stdout/stderr into the ring. Idempotent and best-effort.

    Called by the web server at startup - never at import time - so the CLI's
    one-shot output is untouched.
    """
    global _installed
    if _installed:
        return
    _installed = True
    for name, level in (("stdout", "info"), ("stderr", "error")):
        stream = getattr(sys, name, None)
        if stream is not None and not isinstance(stream, _Tee):
            try:
                setattr(sys, name, _Tee(stream, level))
            except Exception:
                pass
