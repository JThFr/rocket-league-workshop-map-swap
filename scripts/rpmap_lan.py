"""Native LAN discovery relay over Tailscale - productised from a proven PoC.

Rocket League's in-game **Create LAN / Join LAN** discovery broadcasts to
``255.255.255.255:14001``. The kernel routes that out the default NIC, so a
Layer-3 VPN (Tailscale) never sees it - which is why the in-game lobby list stays
empty between tailnet peers. This module relays the broadcast:

    join client --HostQuery--> join relay --unicast--> host relay --inject--> host RL
                                   ^                                              |
                                   | unicast (ServerID rewritten)  HostResponse   |
                                   +---------------- host relay <-----------------+

Discovery is the only thing relayed. Once the peer clicks the lobby, the game
connects straight to ``<host tailnet ip>:7777`` over Tailscale - the same path
Rocket Plugin already proves works - so nothing here touches the game connection.

Protocol (decoded from a live capture):

* ``ProjectX.LanMessage_HostQuery_X``    client -> broadcast:14001
* ``ProjectX.LanMessage_HostResponse_X`` host   -> broadcast:14001 (echoes the query's nonce)
* framing: ``[uint32 BE class-name length][class name][0x00][JSON ... to EOF]``
* the address the client will dial is plaintext ASCII: ``"ServerID":"<ip>:<port>"``

A response echoes the query's ``Nonce``, so the relay is **bidirectional** - a
canned beacon cannot be replayed, it has to round-trip the real query.

Layering: depends on ``rpmap_config`` (the tailnet net) and ``rpmap_peers`` (the
tailnet roster); imported by the CLI (``lan-host`` / ``lan-join``) and by the web
UI (the "LAN discovery relay" panel, via the threaded :class:`Relay`).
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import select
import socket
import threading
import time

from rpmap_config import TAILNET
from rpmap_peers import known_peers, tailscale_self

LAN_PORT = 14001             # RL's LAN discovery (broadcast)
DEFAULT_RELAY_PORT = 14002   # the relay's unicast channel over the tailnet
GAME_PORT = 7777             # the actual game connection (unicast; untouched)
BROADCAST = "255.255.255.255"

MAGIC = b"RLPOC1"            # relay packet magic; the shared token follows it
TYPE_Q = b"Q"                # forwarded HostQuery    (join -> host)
TYPE_R = b"R"                # forwarded HostResponse (host -> join)

_DEDUPE_TTL = 2.0            # seconds a relayed payload is remembered (loop guard)

_SERVERID_RE = re.compile(rb'("ServerID":")([^"]*)(")')
_NONCE_RE = re.compile(rb'"Nonce"\s*:\s*(\d+)')


# --------------------------------------------------------------------------- #
# Rocket League message parsing (pure - the unit-testable core)
# --------------------------------------------------------------------------- #

def classify(data: bytes) -> tuple[str | None, str | None]:
    """Return ``("query"|"response"|None, class_name)`` for a 14001 datagram."""
    if len(data) < 5:
        return None, None
    n = int.from_bytes(data[0:4], "big")
    if n <= 0 or 4 + n > len(data):
        return None, None
    name = data[4:4 + n].decode("ascii", "replace")
    if "HostQuery" in name:
        return "query", name
    if "HostResponse" in name:
        return "response", name
    return None, name


def rewrite_serverid(data: bytes, advertise: str,
                     default_port: int = GAME_PORT) -> tuple[bytes, str, str] | None:
    """Return ``(new_bytes, old_address, new_address)``, or None if no ``ServerID``.

    Only the ``ServerID`` value is touched, so the class-name length prefix is
    unaffected. The JSON grows by a couple of bytes, which is fine: there is no
    outer length field - the datagram boundary *is* the length - and only IP/UDP
    carry checksums, which the kernel rebuilds on send.

    The port is preserved if the host's advertised address carries one, else the
    game port (``7777``) is used.
    """
    m = _SERVERID_RE.search(data)
    if not m:
        return None
    old = m.group(2).decode("ascii", "replace")
    _ip, sep, port = old.rpartition(":")
    if not sep or not port.isdigit():
        port = str(default_port)
    new = f"{advertise}:{port}"
    out = _SERVERID_RE.sub(
        lambda mm: mm.group(1) + new.encode("ascii") + mm.group(3), data)
    return out, old, new


def extract_nonce(data: bytes) -> str | None:
    """The ``Nonce`` value in a query/response, or None (used for logging/tests)."""
    m = _NONCE_RE.search(data)
    return m.group(1).decode("ascii") if m else None


# --------------------------------------------------------------------------- #
# Relay packet framing:  MAGIC | token | 0x00 | type(1) | payload
# --------------------------------------------------------------------------- #

def encode(token: str, typ: bytes, payload: bytes) -> bytes:
    if "\x00" in token:
        raise ValueError("token must not contain NUL")
    return MAGIC + token.encode("ascii") + b"\x00" + typ + payload


def decode(token: str, data: bytes) -> tuple[bytes | None, bytes | None]:
    """Return ``(type, payload)`` for a relay datagram, or ``(None, None)``.

    A packet without our magic or with the wrong shared token is dropped - the
    relay must never inject traffic from an unauthenticated sender.
    """
    if not data.startswith(MAGIC):
        return None, None
    rest = data[len(MAGIC):]
    i = rest.find(b"\x00")
    if i < 0 or len(rest) < i + 2:
        return None, None
    try:
        got = rest[:i].decode("ascii")
    except UnicodeDecodeError:
        return None, None
    if got != token:
        return None, None
    return rest[i + 1:i + 2], rest[i + 2:]


# --------------------------------------------------------------------------- #
# Sockets
# --------------------------------------------------------------------------- #

def make_capture_socket(lan_port: int = LAN_PORT) -> socket.socket:
    """Bind ``0.0.0.0:14001`` alongside Rocket League.

    RL broadcasts to ``255.255.255.255``, which the kernel sends out the
    **default NIC** - never ``tailscale0`` - so the capture socket must listen on
    every interface. On Linux broadcast datagrams are delivered to *all* matching
    sockets, so the game keeps working; ``SO_REUSEADDR`` is what lets us share the
    port (verified on both Linux and Windows).
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("0.0.0.0", lan_port))
    return s


def make_relay_socket(port: int, bind: str) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((bind, port))
    return s


def make_inject_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return s


def inject(sock: socket.socket, data: bytes, mode: str,
           lan_port: int = LAN_PORT) -> None:
    if mode == "loopback":
        sock.sendto(data, ("127.0.0.1", lan_port))
    else:
        sock.sendto(data, (BROADCAST, lan_port))


def source_allowed(ip: str, dev: bool = False) -> bool:
    """True only for a tailnet (or local) sender - never a public/LAN source.

    The relay is private: it must not accept injected packets from anything but
    the tailnet. Loopback is allowed for a same-machine test; ``dev`` (explicit,
    testing without Tailscale) also allows private LAN addresses.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    if dev and addr.is_private:
        return True
    return addr in TAILNET


# --------------------------------------------------------------------------- #
# Peer / address helpers
# --------------------------------------------------------------------------- #

def local_advertise() -> str | None:
    """This machine's Tailscale IPv4, or None if the daemon isn't reachable."""
    ips = tailscale_self().get("ips") or []
    return ips[0] if ips else None


def resolve_peers(cfg: dict, explicit: list[str] | None) -> list[str]:
    """The relay targets: the explicit ``--peer`` list, else online roster peers."""
    if explicit:
        return [p.strip() for p in explicit if p and p.strip()]
    return [p["ip"] for p in known_peers(cfg)
            if p.get("online") and p.get("ip")]


class _Seen:
    """Remember recently relayed payloads so a broadcast loop can't echo forever.

    RL emits several queries/responses with distinct nonces, so a short TTL only
    ever drops an exact duplicate re-arriving within a couple of seconds - the
    "the host relay hears its own injected query" case from the design doc.
    """

    def __init__(self, ttl: float = _DEDUPE_TTL):
        self.ttl = ttl
        self._at: dict[str, float] = {}

    def fresh(self, payload: bytes) -> bool:
        now = time.monotonic()
        for key, at in list(self._at.items()):
            if now - at > self.ttl:
                del self._at[key]
        key = hashlib.sha1(payload).hexdigest()
        if key in self._at:
            return False
        self._at[key] = now
        return True


def _close_all(*socks: socket.socket) -> None:
    for s in socks:
        try:
            s.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #

def run_host(*, peers: list[str], token: str, advertise: str | None = None,
             relay_port: int = DEFAULT_RELAY_PORT, bind: str | None = None,
             inject_mode: str = "broadcast", lan_port: int = LAN_PORT,
             dev: bool = False, log=print, stop_event=None) -> int:
    """Run on the machine hosting the LAN match.

    Captures each ``HostResponse``, rewrites its ``ServerID`` to this machine's
    tailnet IP, and unicasts it to every peer's relay. Blocks until interrupted.
    """
    advertise = advertise or local_advertise()
    if not advertise:
        raise SystemExit("lan-host: no Tailscale IPv4 detected; "
                         "pass --advertise <100.x.y.z>")
    peers = [p for p in peers if p]
    if not peers:
        raise SystemExit("lan-host: no peers; pass --peer <100.x.y.z> "
                         "or bring a peer online on the tailnet")
    bind = bind or advertise

    cap = make_capture_socket(lan_port)
    relay = make_relay_socket(relay_port, bind)
    inj = make_inject_socket()
    seen = _Seen()
    log(f"lan-host: advertise={advertise}  peers={peers}  relay-port={relay_port}  "
        f"bind={bind}  inject={inject_mode}")
    log("lan-host: create a LAN match in Rocket League; waiting for discovery...")
    try:
        while not (stop_event is not None and stop_event.is_set()):
            ready, _, _ = select.select([cap, relay], [], [], 1.0)
            for s in ready:
                data, addr = s.recvfrom(65535)
                if s is cap:
                    kind, _name = classify(data)
                    if kind != "response":
                        continue
                    if not seen.fresh(data):
                        continue
                    res = rewrite_serverid(data, advertise)
                    if res is None:
                        log(f"capture: HostResponse without ServerID from "
                            f"{addr[0]} (ignored)")
                        continue
                    newdata, old, new = res
                    for ip in peers:
                        relay.sendto(encode(token, TYPE_R, newdata),
                                     (ip, relay_port))
                    log(f"capture: HostResponse  {old} -> {new}  -> {peers}")
                else:
                    if not source_allowed(addr[0], dev):
                        log(f"relay: dropped packet from {addr[0]} "
                            f"(not on the tailnet)")
                        continue
                    typ, payload = decode(token, data)
                    if typ is None:
                        log(f"relay: dropped packet from {addr[0]} "
                            f"(magic/token mismatch)")
                        continue
                    if typ == TYPE_Q:
                        if not seen.fresh(payload):
                            continue
                        inject(inj, payload, inject_mode, lan_port)
                        log(f"relay: injected HostQuery from {addr[0]} locally")
                    else:
                        log(f"relay: unexpected type {typ!r} from {addr[0]}")
    except KeyboardInterrupt:
        log("lan-host: stopped")
    finally:
        _close_all(cap, relay, inj)
    return 0


def run_join(*, host: str, token: str, relay_port: int = DEFAULT_RELAY_PORT,
             bind: str | None = None, inject_mode: str = "broadcast",
             lan_port: int = LAN_PORT, dev: bool = False, log=print,
             stop_event=None) -> int:
    """Run on a machine joining the LAN match.

    Captures each ``HostQuery`` the local game broadcasts, unicasts it to the
    host's relay, and re-injects the ``HostResponse`` that comes back. Blocks
    until interrupted.
    """
    bind = bind or local_advertise() or "0.0.0.0"

    cap = make_capture_socket(lan_port)
    relay = make_relay_socket(relay_port, bind)
    inj = make_inject_socket()
    seen = _Seen()
    log(f"lan-join: host={host}  relay-port={relay_port}  bind={bind}  "
        f"inject={inject_mode}")
    log("lan-join: open Rocket League's Join LAN / local lobby list; waiting...")
    try:
        while not (stop_event is not None and stop_event.is_set()):
            ready, _, _ = select.select([cap, relay], [], [], 1.0)
            for s in ready:
                data, addr = s.recvfrom(65535)
                if s is cap:
                    kind, _name = classify(data)
                    if kind != "query":
                        continue
                    if not seen.fresh(data):
                        continue
                    relay.sendto(encode(token, TYPE_Q, data), (host, relay_port))
                    log("capture: forwarded HostQuery -> host")
                else:
                    if not source_allowed(addr[0], dev):
                        log(f"relay: dropped packet from {addr[0]} "
                            f"(not on the tailnet)")
                        continue
                    typ, payload = decode(token, data)
                    if typ is None:
                        log(f"relay: dropped packet from {addr[0]} "
                            f"(magic/token mismatch)")
                        continue
                    if typ == TYPE_R:
                        if not seen.fresh(payload):
                            continue
                        inject(inj, payload, inject_mode, lan_port)
                        log(f"relay: injected HostResponse from {addr[0]} locally")
                    else:
                        log(f"relay: unexpected type {typ!r} from {addr[0]}")
    except KeyboardInterrupt:
        log("lan-join: stopped")
    finally:
        _close_all(cap, relay, inj)
    return 0


# --------------------------------------------------------------------------- #
# Threaded controller (used by the web UI; the CLI runs run_host/run_join directly)
# --------------------------------------------------------------------------- #

class Relay:
    """A LAN discovery relay that runs in a background thread.

    One relay per machine at a time. ``start()`` returns immediately; ``stop()``
    signals the loop and joins it. A fatal setup error (no tailnet address, no
    peers, a busy port) is caught by the thread and exposed as ``error`` instead
    of being raised, so the UI can show it without crashing the server.
    """

    def __init__(self, *, mode: str, token: str, peers: list[str] | None = None,
                 host: str | None = None, advertise: str | None = None,
                 relay_port: int = DEFAULT_RELAY_PORT, bind: str | None = None,
                 inject_mode: str = "broadcast", lan_port: int = LAN_PORT,
                 dev: bool = False, log=print):
        self.mode = mode
        self.token = token
        self.peers = list(peers or [])
        self.host = host
        self.advertise = advertise
        self.relay_port = relay_port
        self.bind = bind
        self.inject_mode = inject_mode
        self.lan_port = lan_port
        self.dev = dev
        self.log = log
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Spawn the relay thread; False if one is already running."""
        if self.running:
            return False
        self._stop.clear()
        self.error = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"rpmap-lan-{self.mode}")
        self._thread.start()
        return True

    def stop(self, timeout: float = 3.0) -> bool:
        """Signal the loop to end and wait for it; True if it stopped cleanly."""
        if self._thread is None:
            return False
        self._stop.set()
        self._thread.join(timeout)
        alive = self._thread.is_alive()
        self._thread = None
        return not alive

    def _run(self) -> None:
        try:
            if self.mode == "join":
                run_join(host=self.host, token=self.token, relay_port=self.relay_port,
                         bind=self.bind, inject_mode=self.inject_mode,
                         lan_port=self.lan_port, dev=self.dev, log=self.log,
                         stop_event=self._stop)
            else:
                run_host(peers=self.peers, token=self.token, advertise=self.advertise,
                         relay_port=self.relay_port, bind=self.bind,
                         inject_mode=self.inject_mode, lan_port=self.lan_port,
                         dev=self.dev, log=self.log, stop_event=self._stop)
        except SystemExit as e:              # run_* raise this for setup errors
            self.error = str(e)
        except Exception as e:               # keep the thread (and the UI) alive
            self.error = str(e)
