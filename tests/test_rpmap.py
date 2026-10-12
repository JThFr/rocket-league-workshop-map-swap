"""Unit + integration tests for rpmap.

Runs with the standard library only:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rpmap.py"

_spec = importlib.util.spec_from_file_location("rpmap", SCRIPT)
rpmap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rpmap)

SLOT_FILE = rpmap.SLOTS["underpass"][0]


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
        self.assertGreater(rpmap._version_tuple("v0.10.0"), rpmap._version_tuple("0.9.9"))
        self.assertEqual(rpmap._version_tuple("0.3.0"), (0, 3, 0))

    def test_app_version_shape(self):
        self.assertRegex(rpmap.APP_VERSION, r"^\d+\.\d+\.\d+$")

    def test_asset_name_is_known(self):
        self.assertIn(rpmap._update_asset_name(),
                      {"rpmap.exe", "rpmap-x86_64.AppImage"})

    def test_secret_format(self):
        self.assertRegex(rpmap.new_secret(), r"^[0-9a-f]{4}(-[0-9a-f]{4}){3}$")


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
            self.assertEqual(rpmap._update_target(), (Path(f.name), "appimage"))

    def test_no_target_when_running_from_source(self):
        self.assertIsNone(rpmap._update_target())


class SlotTests(_CookedFixture):
    def test_slot_reports_stock_when_backup_matches(self):
        (self.cooked / (SLOT_FILE + ".orig")).write_bytes(b"STOCK")
        self.assertEqual(rpmap.slot_state(self.cooked, "underpass")["state"], "stock")

    def test_use_then_restore_roundtrip(self):
        rpmap.use_map(self.cooked, "underpass", self.cooked / "mods" / "TestMap.upk")
        state = rpmap.slot_state(self.cooked, "underpass")
        self.assertEqual(state["state"], "custom")
        self.assertEqual(state["map_name"], "TestMap")
        self.assertTrue(state["backup_exists"])

        rpmap.restore_slot(self.cooked, "underpass")
        self.assertEqual(rpmap.slot_state(self.cooked, "underpass")["state"], "stock")

    def test_use_refuses_custom_slot_without_backup(self):
        (self.cooked / SLOT_FILE).write_bytes(b"TESTMAP")   # matches mods/TestMap.upk
        self.assertEqual(rpmap.slot_state(self.cooked, "underpass")["state"], "custom")
        with self.assertRaises(RuntimeError):
            rpmap.use_map(self.cooked, "underpass", self.cooked / "mods" / "TestMap.upk")

    def test_file_hash_matches_for_identical_bytes(self):
        a = self.cooked / "mods" / "TestMap.upk"
        b = self.cooked / "mods" / "Copy.upk"
        b.write_bytes(a.read_bytes())
        self.assertEqual(rpmap.file_hash(a), rpmap.file_hash(b))

    def test_slot_state_reports_arena_name(self):
        st = rpmap.slot_state(self.cooked, "underpass")
        self.assertEqual(st["arena"], "Underpass")
        self.assertEqual(st["note"], 'NOT "Tokyo Underpass"')
        self.assertEqual(rpmap.slot_state(self.cooked, "utopia")["arena"], "Utopia Retro")


class RestoreOnExitTests(_CookedFixture):
    def test_restores_a_swapped_slot(self):
        rpmap.use_map(self.cooked, "underpass", self.cooked / "mods" / "TestMap.upk")
        self.assertEqual(rpmap.slot_state(self.cooked, "underpass")["state"], "custom")
        rpmap._restore_on_exit(self.cooked, "underpass")
        self.assertEqual(rpmap.slot_state(self.cooked, "underpass")["state"], "stock")

    def test_is_a_noop_without_a_backup(self):
        rpmap._restore_on_exit(self.cooked, "underpass")   # must not raise
        self.assertEqual(rpmap.slot_state(self.cooked, "underpass")["state"], "unknown")


class MapsTests(_CookedFixture):
    def test_list_maps_finds_mods(self):
        self.assertIn("TestMap", {m["name"] for m in rpmap.list_maps(self.cooked)})

    def test_resolve_map_by_name_is_case_insensitive(self):
        m = rpmap.resolve_map(self.cooked, "testmap")
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
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), rpmap.make_handler(engine))
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
