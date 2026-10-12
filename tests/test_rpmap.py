"""Unit + integration tests for rpmap.

Runs with the standard library only:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import http.client
import json
import os
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rpmap_config as config
import rpmap_doctor as doctor
import rpmap_maps as maps
import rpmap_peers as peers
import rpmap_update as update
import rpmap_web as web
import rpmap_window as window

SLOT_FILE = config.SLOTS["underpass"][0]
PACKAGE_TAG = 0x9E2A83C1


def _package(licensee: int) -> bytes:
    """Minimal bytes that look like a cooked UE3 package header."""
    return struct.pack("<IHH", PACKAGE_TAG, 868, licensee) + b"\x00" * 32


def setUpModule() -> None:
    # Isolate config/cache/map discovery from the developer's real home.
    tmp = tempfile.mkdtemp(prefix="rpmap-test-")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "config")
    os.environ["XDG_CACHE_HOME"] = os.path.join(tmp, "cache")
    os.environ["RL_MAPS"] = os.path.join(tmp, "maps")
    os.makedirs(os.environ["RL_MAPS"], exist_ok=True)


class _CookedFixture(unittest.TestCase):
    """A throwaway CookedPCConsole with a stock slot and one map in mods/."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rpmap-cooked-")
        self.cooked = Path(self._tmp.name) / "TAGame" / "CookedPCConsole"
        (self.cooked / "mods").mkdir(parents=True)
        (self.cooked / SLOT_FILE).write_bytes(b"STOCK")
        (self.cooked / "mods" / "TestMap.upk").write_bytes(b"TESTMAP")

    def tearDown(self) -> None:
        self._tmp.cleanup()


class VersionTests(unittest.TestCase):
    def test_ordering(self):
        self.assertGreater(update._version_tuple("v0.10.0"), update._version_tuple("0.9.9"))
        self.assertEqual(update._version_tuple("0.3.0"), (0, 3, 0))

    def test_app_version_shape(self):
        self.assertRegex(config.APP_VERSION, r"^\d+\.\d+\.\d+$")

    def test_asset_name_is_known(self):
        self.assertIn(update._update_asset_name(),
                      {"rpmap.exe", "rpmap-x86_64.AppImage"})

    def test_secret_format(self):
        self.assertRegex(config.new_secret(), r"^[0-9a-f]{4}(-[0-9a-f]{4}){3}$")


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self._appimage = os.environ.pop("APPIMAGE", None)

    def tearDown(self):
        if self._appimage is None:
            os.environ.pop("APPIMAGE", None)
        else:
            os.environ["APPIMAGE"] = self._appimage

    def test_appimage_env_selects_deferred_swap(self):
        with tempfile.NamedTemporaryFile(suffix=".AppImage") as f:
            os.environ["APPIMAGE"] = f.name
            self.assertEqual(update._update_target(), (Path(f.name), "appimage"))

    def test_no_target_when_running_from_source(self):
        self.assertIsNone(update._update_target())

    def test_posix_swap_relaunches_with_our_flags(self):
        script = update._posix_swap_script(Path("/tmp/new"), Path("/tmp/app.AppImage"),
                                           ["--slot", "underpass", "--restore-on-exit",
                                            "/a path/with space"])
        self.assertIn("nohup /tmp/app.AppImage --slot underpass --restore-on-exit "
                      "'/a path/with space'", script)
        self.assertIn(">/dev/null 2>&1 &", script)

    def test_posix_swap_with_no_flags(self):
        script = update._posix_swap_script(Path("/tmp/new"), Path("/tmp/app.AppImage"), [])
        self.assertIn("nohup /tmp/app.AppImage >/dev/null 2>&1 &", script)

    def test_windows_swap_forwards_its_arguments(self):
        script = update._windows_swap_script(Path(r"C:\new.exe"), Path(r"C:\app.exe"))
        self.assertIn('start "" "C:\\app.exe" %*', script)

    def test_refusal_for_foreign_url(self):
        info = {"url": "https://example.com/rpmap.exe", "digest": "sha256:" + "a" * 64}
        reason = update._update_refusal(info)
        self.assertIsNotNone(reason)
        self.assertIn("official", reason)

    def test_refusal_for_missing_digest(self):
        info = {"url": f"https://github.com/{config.GITHUB_REPO}/releases/download/"
                       f"v0.0.0/rpmap.exe", "digest": None}
        reason = update._update_refusal(info)
        self.assertIsNotNone(reason)
        self.assertIn("digest", reason)

    def test_refusal_for_malformed_digest(self):
        info = {"url": f"https://github.com/{config.GITHUB_REPO}/releases/download/"
                       f"v0.0.0/rpmap.exe", "digest": "sha256:zz"}
        reason = update._update_refusal(info)
        self.assertIsNotNone(reason)
        self.assertIn("digest", reason)

    def test_no_refusal_for_good_info(self):
        info = {"url": f"https://github.com/{config.GITHUB_REPO}/releases/download/"
                       f"v0.0.0/rpmap.exe", "digest": "a" * 64}
        self.assertIsNone(update._update_refusal(info))

    def test_download_update_checks_size(self):
        data = b"NEWBUILD"
        with tempfile.TemporaryDirectory(prefix="rpmap-dl-") as d:
            src = Path(d) / "new-build"
            src.write_bytes(data)
            dest = Path(d) / "dest"
            update._download_update(f"file://{src}", dest, None, size=len(data))
            self.assertEqual(dest.read_bytes(), data)
            with self.assertRaises(RuntimeError):
                update._download_update(f"file://{src}", dest, None, size=len(data) + 1)
            self.assertFalse(dest.exists())

    def test_download_update_checks_digest(self):
        import hashlib
        data = b"NEWBUILD"
        with tempfile.TemporaryDirectory(prefix="rpmap-dl-") as d:
            src = Path(d) / "new-build"
            src.write_bytes(data)
            dest = Path(d) / "dest"
            good = hashlib.sha256(data).hexdigest()
            update._download_update(f"file://{src}", dest, good, size=len(data))
            self.assertEqual(dest.read_bytes(), data)
            with self.assertRaises(RuntimeError):
                update._download_update(f"file://{src}", dest, "b" * 64, size=len(data))
            self.assertFalse(dest.exists())

    @unittest.skipIf(os.name == "nt", "POSIX mkdtemp/chmod permissions")
    def test_posix_swap_script_lives_in_a_private_dir(self):
        import shutil
        script = update._write_posix_swap(Path("/tmp/new"), Path("/tmp/app.AppImage"), [])
        try:
            self.assertTrue(script.parent.name.startswith("rpmap-update-"))
            self.assertNotEqual(script, Path(tempfile.gettempdir()) / "rpmap-update.sh")
            self.assertEqual(stat.S_IMODE(os.stat(script).st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(os.stat(script.parent).st_mode), 0o700)
        finally:
            shutil.rmtree(script.parent, ignore_errors=True)

    def test_windows_swap_script_lives_in_a_private_dir(self):
        import shutil
        script = update._write_windows_swap(Path(r"C:\new.exe"), Path(r"C:\app.exe"), [])
        try:
            self.assertTrue(script.parent.name.startswith("rpmap-update-"))
            self.assertNotEqual(script,
                                Path(tempfile.gettempdir()) / "rpmap-update.cmd")
            self.assertEqual(script.name, "swap.cmd")
        finally:
            shutil.rmtree(script.parent, ignore_errors=True)


class CaBundleTests(unittest.TestCase):
    """The packaged build must be able to find a CA bundle for TLS."""

    def setUp(self):
        self._saved = os.environ.pop("SSL_CERT_FILE", None)
        self._orig = config.CA_BUNDLE_CANDIDATES

    def tearDown(self):
        config.CA_BUNDLE_CANDIDATES = self._orig
        if self._saved is None:
            os.environ.pop("SSL_CERT_FILE", None)
        else:
            os.environ["SSL_CERT_FILE"] = self._saved

    @unittest.skipIf(os.name == "nt", "POSIX CA-bundle fallback")
    def test_picks_the_first_existing_bundle(self):
        with tempfile.TemporaryDirectory() as d:
            good = Path(d) / "ca.pem"
            good.write_text("x")
            config.CA_BUNDLE_CANDIDATES = (str(Path(d) / "missing.pem"), str(good))
            self.assertEqual(config.ensure_ca_bundle(), str(good))
            self.assertEqual(os.environ.get("SSL_CERT_FILE"), str(good))

    @unittest.skipIf(os.name == "nt", "POSIX CA-bundle fallback")
    def test_respects_an_existing_setting(self):
        os.environ["SSL_CERT_FILE"] = "/custom/ca.pem"
        config.CA_BUNDLE_CANDIDATES = ("/etc/ssl/certs/ca-certificates.crt",)
        self.assertIsNone(config.ensure_ca_bundle())
        self.assertEqual(os.environ["SSL_CERT_FILE"], "/custom/ca.pem")


class PeerTests(unittest.TestCase):
    def test_common_map_keys(self):
        self.assertEqual(peers.common_map_keys([{"a", "b"}, {"b", "c"}]), ["b"])

    def test_common_map_keys_with_nothing_to_intersect(self):
        self.assertIsNone(peers.common_map_keys([]))
        self.assertIsNone(peers.common_map_keys([None, None]))

    def test_common_map_keys_ignores_peers_without_inventory(self):
        self.assertEqual(peers.common_map_keys([{"a"}, None]), ["a"])


class SyncTargetTests(_CookedFixture):
    """`sync_action` only contacts peers on the discovered roster.

    Without that check `/api/sync` would connect to any ip:port a caller
    names - a request proxy that also hands the session secret to whoever
    the target happens to be.
    """

    def setUp(self):
        super().setUp()
        self._orig = peers.tailscale_peers
        peers.tailscale_peers = lambda cfg=None: [
            {"peer_id": "friend", "name": "friend", "dns": "", "ip": "100.64.0.7",
             "port": None, "online": True, "os": "linux"},
            {"peer_id": "manual", "name": "manual", "dns": "", "ip": "10.1.2.3",
             "port": 9000, "online": True, "os": "linux"},
        ]

    def tearDown(self):
        peers.tailscale_peers = self._orig
        super().tearDown()

    def test_roster_targets_map_ips_to_ports(self):
        allowed = peers._roster_targets({}, 8787)
        self.assertEqual(allowed, {"100.64.0.7": 8787, "10.1.2.3": 9000})

    def test_target_outside_the_roster_is_refused(self):
        res = peers.sync_action(self.cooked, "underpass", {"session_secret": "s"},
                                "push", [{"ip": "203.0.113.9", "port": 8787,
                                          "name": "evil"}])
        self.assertFalse(res["ok"])
        self.assertEqual(res["results"][0]["error"], "not a known peer")

    def test_wrong_port_for_a_roster_peer_is_refused(self):
        res = peers.sync_action(self.cooked, "underpass", {"session_secret": "s"},
                                "push", [{"ip": "100.64.0.7", "port": 9999,
                                          "name": "friend"}])
        self.assertFalse(res["ok"])
        self.assertEqual(res["results"][0]["error"], "not a known peer")

    def test_roster_peer_on_its_port_passes_the_gate(self):
        # A roster peer at its own port gets past the gate; the connection
        # then fails as a peer-level error (dead loopback port), never as
        # "not a known peer".
        dead = _free_port()
        peers.tailscale_peers = lambda cfg=None: [
            {"peer_id": "friend", "name": "friend", "dns": "", "ip": "127.0.0.1",
             "port": dead, "online": True, "os": "linux"}]
        res = peers.sync_action(self.cooked, "underpass", {"session_secret": "s"},
                                "push", [{"ip": "127.0.0.1", "port": dead,
                                          "name": "friend"}])
        self.assertFalse(res["ok"])
        self.assertNotEqual(res["results"][0].get("error"), "not a known peer")


class SlotTests(_CookedFixture):
    def test_slot_reports_stock_when_backup_matches(self):
        (self.cooked / (SLOT_FILE + ".orig")).write_bytes(b"STOCK")
        self.assertEqual(maps.slot_state(self.cooked, "underpass")["state"], "stock")

    def test_use_then_restore_roundtrip(self):
        maps.use_map(self.cooked, "underpass", self.cooked / "mods" / "TestMap.upk")
        state = maps.slot_state(self.cooked, "underpass")
        self.assertEqual(state["state"], "custom")
        self.assertEqual(state["map_name"], "TestMap")
        self.assertTrue(state["backup_exists"])

        maps.restore_slot(self.cooked, "underpass")
        self.assertEqual(maps.slot_state(self.cooked, "underpass")["state"], "stock")

    def test_use_refuses_custom_slot_without_backup(self):
        (self.cooked / SLOT_FILE).write_bytes(b"TESTMAP")   # matches mods/TestMap.upk
        self.assertEqual(maps.slot_state(self.cooked, "underpass")["state"], "custom")
        with self.assertRaises(RuntimeError):
            maps.use_map(self.cooked, "underpass", self.cooked / "mods" / "TestMap.upk")

    def test_stock_slot_without_backup_is_recognised(self):
        # The default slot (Utopia) usually has no backup yet; the game file's
        # package header must still identify it as stock, not "unknown".
        (self.cooked / SLOT_FILE).write_bytes(_package(34))
        st = maps.slot_state(self.cooked, "underpass")
        self.assertEqual(st["state"], "stock")
        self.assertFalse(st["backup_exists"])
        self.assertTrue(st["inferred"])

    def test_swapped_slot_without_backup_is_custom(self):
        # A licensee-0 cook is a community map, even when it isn't in the list.
        (self.cooked / SLOT_FILE).write_bytes(_package(0))
        st = maps.slot_state(self.cooked, "underpass")
        self.assertEqual(st["state"], "custom")
        self.assertIsNone(st["map_name"])
        with self.assertRaises(RuntimeError):   # must not back up a custom map as stock
            maps.use_map(self.cooked, "underpass", self.cooked / "mods" / "TestMap.upk")

    def test_garbage_slot_file_stays_unknown(self):
        (self.cooked / SLOT_FILE).write_bytes(b"not a UE3 package")
        self.assertEqual(maps.slot_state(self.cooked, "underpass")["state"], "unknown")

    def test_file_hash_matches_for_identical_bytes(self):
        a = self.cooked / "mods" / "TestMap.upk"
        b = self.cooked / "mods" / "Copy.upk"
        b.write_bytes(a.read_bytes())
        self.assertEqual(maps.file_hash(a), maps.file_hash(b))

    def test_slot_state_reports_arena_name(self):
        st = maps.slot_state(self.cooked, "underpass")
        self.assertEqual(st["arena"], "Underpass")
        self.assertEqual(st["note"], 'NOT "Tokyo Underpass"')
        self.assertEqual(maps.slot_state(self.cooked, "utopia")["arena"], "Utopia Retro")


class RestoreOnExitTests(_CookedFixture):
    def test_restores_a_swapped_slot(self):
        maps.use_map(self.cooked, "underpass", self.cooked / "mods" / "TestMap.upk")
        self.assertEqual(maps.slot_state(self.cooked, "underpass")["state"], "custom")
        web._restore_on_exit(self.cooked, "underpass")
        self.assertEqual(maps.slot_state(self.cooked, "underpass")["state"], "stock")

    def test_is_a_noop_without_a_backup(self):
        web._restore_on_exit(self.cooked, "underpass")   # must not raise
        self.assertEqual(maps.slot_state(self.cooked, "underpass")["state"], "unknown")


class MapsTests(_CookedFixture):
    def test_list_maps_finds_mods(self):
        self.assertIn("TestMap", {m["name"] for m in maps.list_maps(self.cooked)})

    def test_resolve_map_by_name_is_case_insensitive(self):
        m = maps.resolve_map(self.cooked, "testmap")
        self.assertIsNotNone(m)
        self.assertEqual(m["name"], "TestMap")

    def test_map_key_prefers_workshop_id(self):
        self.assertEqual(maps.map_key({"workshop_id": "123", "name": "X", "size": 9}), "w:123")

    def test_map_key_falls_back_to_name_and_size(self):
        self.assertEqual(maps.map_key({"workshop_id": None, "name": "My Map", "size": 42}),
                         "n:my map:42")

    def test_map_inventory_entries_have_keys(self):
        inv = maps.map_inventory(self.cooked)
        self.assertIn("TestMap", {e["name"] for e in inv})
        self.assertTrue(all("key" in e for e in inv))


class HttpTests(_CookedFixture):
    """Drive the real HTTP server over a socket."""

    TOKEN = "test-token"

    def setUp(self) -> None:
        super().setUp()
        self.quit_called = threading.Event()
        engine = {
            "cooked": self.cooked, "slot": "underpass", "token": self.TOKEN,
            "host": "127.0.0.1", "secret": "s3cr3t", "cfg": {},
            "ident": {"peer_id": "tester", "name": "tester", "ips": [], "dns": ""},
            "dev": False, "port": 0, "request_quit": self.quit_called.set,
        }
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(engine))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def _req(self, method, path, body=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(data))
        if host is not None:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for key, value in headers.items():
                conn.putheader(key, value)
            conn.putheader("Host", host)
            conn.endheaders(data)
        else:
            conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp.status, payload

    def test_index_serves_the_ui(self):
        status, body = self._req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"rpmap", body.lower())

    def test_api_requires_token(self):
        self.assertEqual(self._req("GET", "/api/state")[0], 403)
        self.assertEqual(self._req("GET", "/api/state?token=" + self.TOKEN)[0], 200)

    def test_capabilities_reports_product(self):
        status, body = self._req("GET", "/api/capabilities?token=" + self.TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["product"], "rpmap")

    def test_api_maps_lists_inventory(self):
        status, body = self._req("GET", "/api/maps?token=" + self.TOKEN)
        self.assertEqual(status, 200)
        inv = json.loads(body)["maps"]
        self.assertIn("TestMap", {e["name"] for e in inv})
        self.assertTrue(all("key" in e for e in inv))

    def test_state_maps_carry_a_key(self):
        _, body = self._req("GET", "/api/state?token=" + self.TOKEN)
        self.assertTrue(all("key" in m for m in json.loads(body)["maps"]))

    def test_unknown_host_is_rejected(self):
        status, _ = self._req("GET", "/api/state?token=" + self.TOKEN, host="evil.example")
        self.assertEqual(status, 403)

    def test_unknown_api_path_returns_json_404(self):
        status, body = self._req("GET", "/api/nope?token=" + self.TOKEN)
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"], "not found")

    def test_use_and_restore_over_http(self):
        _, body = self._req("GET", "/api/state?token=" + self.TOKEN)
        mid = next(m["id"] for m in json.loads(body)["maps"] if m["name"] == "TestMap")

        status, body = self._req("POST", "/api/use?token=" + self.TOKEN, body={"id": mid})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

        _, body = self._req("GET", "/api/state?token=" + self.TOKEN)
        self.assertEqual(json.loads(body)["slot"]["state"], "custom")

        status, _ = self._req("POST", "/api/restore?token=" + self.TOKEN)
        self.assertEqual(status, 200)
        _, body = self._req("GET", "/api/state?token=" + self.TOKEN)
        self.assertEqual(json.loads(body)["slot"]["state"], "stock")

    def test_quit_requires_the_token(self):
        self.assertEqual(self._req("POST", "/api/quit")[0], 403)

    def test_quit_calls_the_injected_shutdown(self):
        status, body = self._req("POST", "/api/quit?token=" + self.TOKEN)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertTrue(self.quit_called.wait(2), "request_quit was never called")

    def test_session_rotation_is_allowed_from_loopback(self):
        status, body = self._req("POST", "/api/session?token=" + self.TOKEN,
                                 body={"secret": "a-new-code"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["secret"], "a-new-code")

    def test_responses_carry_hardening_headers(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        headers = {k.lower(): v.lower() for k, v in resp.getheaders()}
        resp.read()
        conn.close()
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(headers.get("referrer-policy"), "no-referrer")
        self.assertIn("frame-ancestors 'none'",
                      headers.get("content-security-policy", ""))

    def test_ui_never_interpolates_ids_into_js_source(self):
        # Ids travel as data attributes to a delegated listener; inline
        # "onclick=\"fn('${id}')\"" is an injection trap (HTML entities decode
        # before the JS engine sees the string).
        _, body = self._req("GET", "/")
        self.assertIn(b"data-peer-id=", body)
        self.assertIn(b"addEventListener('click'", body)
        self.assertIn(b'name="referrer" content="no-referrer"', body)
        self.assertNotIn(b'onclick="useMap(', body)
        self.assertNotIn(b'onclick="syncFrom(', body)
        self.assertNotIn(b'onclick="syncTo(', body)

    def test_running_instance_finds_the_live_server(self):
        config.write_runtime({"pid": os.getpid(), "port": self.port,
                              "token": self.TOKEN})
        rec = web.running_instance()
        self.assertIsNotNone(rec)
        self.assertEqual(rec["port"], self.port)
        config.clear_runtime(os.getpid())

    def test_running_instance_ignores_a_stale_record(self):
        config.write_runtime({"pid": 10_000_000, "port": self.port,
                              "token": self.TOKEN})
        self.assertIsNone(web.running_instance())
        config.clear_runtime(10_000_000)


class EveryoneFilterTests(unittest.TestCase):
    """Two servers with different inventories; one sees the other as a peer."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rpmap-everyone-")
        root = Path(self._tmp.name)
        self.cooked1 = root / "m1" / "TAGame" / "CookedPCConsole"
        self.cooked2 = root / "m2" / "TAGame" / "CookedPCConsole"
        for cooked, extra in ((self.cooked1, True), (self.cooked2, False)):
            (cooked / "mods").mkdir(parents=True)
            (cooked / "mods" / "Shared.upk").write_bytes(b"SHARED")
            if extra:
                (cooked / "mods" / "Only1.upk").write_bytes(b"ONLY1")
        self.srv1 = self._serve(self.cooked1)

    def _serve(self, cooked):
        engine = {"cooked": cooked, "slot": "underpass", "token": "t",
                  "host": "127.0.0.1", "secret": "s3cr3t", "cfg": {},
                  "ident": {"peer_id": "x", "name": "x", "ips": [], "dns": ""},
                  "dev": False, "port": 0}
        srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(engine))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    def tearDown(self):
        self.srv1.shutdown()
        self.srv1.server_close()
        self._tmp.cleanup()

    def test_inventory_and_common(self):
        p1 = self.srv1.server_address[1]

        got = peers.probe_peer("127.0.0.1", p1, "s3cr3t")
        self.assertIsNotNone(got)
        self.assertEqual({e["name"] for e in got["maps"]}, {"Shared", "Only1"})

        orig = peers.tailscale_peers
        peers.tailscale_peers = lambda cfg=None: [
            {"peer_id": "m1", "name": "m1", "dns": "", "ip": "127.0.0.1",
             "port": p1, "online": True, "os": "linux"}]
        try:
            data = peers.gather_peers(self.cooked2, "underpass",
                                      {"session_secret": "s3cr3t"}, {})
        finally:
            peers.tailscale_peers = orig

        # "everyone" = this machine AND every reachable peer, so `common` is the
        # intersection including our own inventory (shared here), not just the
        # peer's whole inventory.
        self.assertEqual(data["peers"][0]["map_count"], 2)
        self.assertEqual(set(data["common"]), {"n:shared:6"})
        self.assertEqual(data["peers"][0]["reach"], "ok")
        local_keys = {maps.map_key(m) for m in maps.list_maps(self.cooked2)}
        self.assertEqual(local_keys & set(data["common"]), {"n:shared:6"})

    def test_everyone_includes_this_machine_when_alone(self):
        """With no peers the filter is still usable: 'everyone' is just us."""
        orig = peers.tailscale_peers
        peers.tailscale_peers = lambda cfg=None: []
        try:
            data = peers.gather_peers(self.cooked2, "underpass",
                                      {"session_secret": "s3cr3t"}, {})
        finally:
            peers.tailscale_peers = orig

        self.assertEqual(data["peers"], [])
        local_keys = {maps.map_key(m) for m in maps.list_maps(self.cooked2)}
        self.assertIsNotNone(data["common"], "filter should be available alone")
        self.assertEqual(set(data["common"]), local_keys)

    def test_unreachable_reasons(self):
        """A tailnet node that isn't serving rpmap gets a clear reason."""
        orig = peers.tailscale_peers
        dead = _free_port()          # nothing is listening here
        peers.tailscale_peers = lambda cfg=None: [
            {"peer_id": "off", "name": "off", "dns": "", "ip": "127.0.0.1",
             "port": dead, "online": False, "os": "windows"},
            {"peer_id": "up", "name": "up", "dns": "", "ip": "127.0.0.1",
             "port": dead, "online": True, "os": "windows"},
        ]
        try:
            data = peers.gather_peers(self.cooked2, "underpass",
                                      {"session_secret": "s3cr3t"}, {})
        finally:
            peers.tailscale_peers = orig
        reach = {r["name"]: r["reach"] for r in data["peers"]}
        self.assertEqual(reach["off"], "offline")
        self.assertEqual(reach["up"], "not-rpmap")


class QuitGateTests(unittest.TestCase):
    """The local-only gates (/api/quit, /api/session) as plain predicates."""

    def _handler(self, ident=None):
        return web.make_handler({"cooked": Path("."), "slot": "underpass",
                                 "token": "t", "host": "127.0.0.1", "secret": "s",
                                 "cfg": {}, "ident": ident or {}, "dev": False,
                                 "port": 0})

    def test_loopback_is_allowed(self):
        class _Conn:
            client_address = ("127.0.0.1", 51234)
        self.assertTrue(self._handler()._local_ok(_Conn()))

    def test_tailnet_peer_is_refused(self):
        class _Conn:
            client_address = ("100.64.0.7", 51234)
        self.assertFalse(self._handler()._local_ok(_Conn()))

    def test_own_tailnet_address_is_allowed(self):
        # A UI served on the tailnet IP is reached by the local browser from
        # that same address - still "this machine", still allowed to quit or
        # rotate the session code.
        class _Conn:
            client_address = ("100.64.0.7", 51234)
        handler = self._handler(ident={"peer_id": "me", "name": "me",
                                       "ips": ["100.64.0.7"], "dns": ""})
        self.assertTrue(handler._local_ok(_Conn()))

    def test_remote_peer_on_our_tailnet_is_refused(self):
        class _Conn:
            client_address = ("100.64.0.9", 51234)
        handler = self._handler(ident={"peer_id": "me", "name": "me",
                                       "ips": ["100.64.0.7"], "dns": ""})
        self.assertFalse(handler._local_ok(_Conn()))


class RuntimeFileTests(unittest.TestCase):
    """The runtime record: round-trip, permissions, pid-guarded removal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rpmap-runtime-")
        self._old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._old
        self.tmp.cleanup()

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(config.read_runtime(), {})

    def test_roundtrip_and_permissions(self):
        config.write_runtime({"pid": os.getpid(), "port": 8787, "token": "abc"})
        rec = config.read_runtime()
        self.assertEqual(rec["port"], 8787)
        self.assertEqual(rec["token"], "abc")
        if os.name != "nt":
            mode = stat.S_IMODE(os.stat(config.runtime_path()).st_mode)
            self.assertEqual(mode, 0o600)
        config.clear_runtime(os.getpid())
        self.assertEqual(config.read_runtime(), {})

    def test_clear_keeps_another_instances_record(self):
        config.write_runtime({"pid": os.getpid(), "port": 1})
        config.clear_runtime(10_000_000)          # a different (dead) pid
        self.assertEqual(config.read_runtime()["port"], 1)

    def test_pid_alive(self):
        self.assertTrue(config.pid_alive(os.getpid()))
        self.assertFalse(config.pid_alive(10_000_000))


class WindowTests(unittest.TestCase):
    """The status window is best-effort: never import- or runtime-fatal."""

    def test_available_returns_a_bool(self):
        self.assertIsInstance(window.available(), bool)

    def test_no_window_without_a_display(self):
        if os.name == "nt":
            self.skipTest("the DISPLAY check is POSIX-only")
        old = os.environ.pop("DISPLAY", None)
        try:
            self.assertFalse(window.available())
        finally:
            if old is not None:
                os.environ["DISPLAY"] = old


class DoctorTests(unittest.TestCase):
    """`rpmap doctor` never raises and flags a missing game install."""

    def test_missing_rocket_league_exits_nonzero(self):
        self.assertEqual(doctor.run("/nonexistent/definitely-not-rocketleague", "underpass"), 1)

    def test_healthy_fixture_exits_zero(self):
        with tempfile.TemporaryDirectory(prefix="rpmap-doctor-") as d:
            cooked = Path(d) / "TAGame" / "CookedPCConsole"
            (cooked / "mods").mkdir(parents=True)
            (cooked / config.SLOTS["underpass"][0]).write_bytes(b"STOCK")
            saved = (doctor.peers.tailscale_peers, doctor.peers.tailscale_self,
                     doctor.update.check_for_update)
            doctor.peers.tailscale_peers = lambda cfg=None: []      # no network
            doctor.peers.tailscale_self = lambda: {"ips": []}
            doctor.update.check_for_update = lambda: None
            try:
                self.assertEqual(doctor.run(str(cooked), "underpass"), 0)
            finally:
                (doctor.peers.tailscale_peers, doctor.peers.tailscale_self,
                 doctor.update.check_for_update) = saved


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerLifecycleTests(_CookedFixture):
    """A real run_server: writes the runtime file, /api/quit stops it cleanly."""

    def test_quit_stops_the_server_and_clears_runtime(self):
        port = _free_port()
        old_ident = web.local_identity
        web.local_identity = lambda cfg: {"peer_id": "t", "name": "t",
                                          "ips": [], "dns": ""}
        try:
            thread = threading.Thread(
                target=web.run_server,
                args=(self.cooked, "underpass", "127.0.0.1", port, False),
                kwargs={"secret": "s3cr3t", "window": False}, daemon=True)
            thread.start()

            rec = {}
            for _ in range(100):                  # wait for run_server to bind
                rec = config.read_runtime()
                if rec.get("port") and rec.get("pid") == os.getpid():
                    break
                time.sleep(0.05)
            self.assertTrue(rec.get("port"), "run_server never wrote the runtime file")

            conn = http.client.HTTPConnection("127.0.0.1", rec["port"], timeout=5)
            conn.request("POST", "/api/quit?token=" + rec["token"])
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertTrue(json.loads(resp.read())["ok"])
            conn.close()

            thread.join(5)
            self.assertFalse(thread.is_alive(), "the server did not stop")
            self.assertEqual(config.read_runtime(), {})
        finally:
            web.local_identity = old_ident


if __name__ == "__main__":
    unittest.main()
