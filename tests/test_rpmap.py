"""Unit + integration tests for rpmap.

Runs with the standard library only:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import http.client
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rpmap_config as config
import rpmap_maps as maps
import rpmap_update as update
import rpmap_web as web

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


class HttpTests(_CookedFixture):
    """Drive the real HTTP server over a socket."""

    TOKEN = "test-token"

    def setUp(self) -> None:
        super().setUp()
        engine = {
            "cooked": self.cooked, "slot": "underpass", "token": self.TOKEN,
            "host": "127.0.0.1", "secret": "s3cr3t", "cfg": {},
            "ident": {"peer_id": "tester", "name": "tester", "ips": [], "dns": ""},
            "dev": False, "port": 0,
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


if __name__ == "__main__":
    unittest.main()
