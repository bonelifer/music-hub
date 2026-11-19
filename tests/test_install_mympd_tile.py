"""Offline tests for install-mympd-tile.py against a fake myMPD API."""

import importlib.util
import json
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "install-mympd-tile.py"
SPEC = importlib.util.spec_from_file_location("install_mympd_tile", SCRIPT)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class FakeMympd(BaseHTTPRequestHandler):
    """Answer the two Home tile methods and keep the tiles in memory."""

    tiles: list = []
    sessions: list = []

    def do_POST(self):  # noqa: N802 - name required by BaseHTTPRequestHandler
        FakeMympd.sessions.append(self.headers.get("X-myMPD-Session"))
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        params = request["params"]
        if request["method"] == "MYMPD_API_HOME_ICON_LIST":
            payload = {"result": {"data": self.tiles}}
        elif params["name"] == "refuse":
            payload = {"error": {"message": "no"}}
        else:
            tile = {key: params[key] for key in
                    ("name", "ligature", "bgcolor", "color", "image",
                     "cmd", "options")}
            if params["replace"]:
                self.tiles[params["oldpos"]] = tile
            else:
                self.tiles.append(tile)
            payload = {"result": {"message": "ok"}}
        body = json.dumps({"jsonrpc": "2.0", "id": 1, **payload}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Keep test output quiet."""


class InstallTileTest(unittest.TestCase):
    """Check that the tile is added once and then updated in place."""

    def setUp(self):
        FakeMympd.tiles = [{"name": "Other", "cmd": "openExternalLink",
                            "options": ["http://example.test/", "true"]}]
        FakeMympd.sessions = []
        self.server = HTTPServer(("127.0.0.1", 0), FakeMympd)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/api/default"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_adds_then_updates_without_duplicating(self):
        self.assertFalse(installer.install(self.endpoint, "http://a.test:18773/"))
        self.assertTrue(installer.install(self.endpoint, "http://b.test:18773/"))
        ours = [tile for tile in FakeMympd.tiles if tile["name"] == "music-hub"]
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0]["options"], ["http://b.test:18773/", "true"])
        self.assertEqual(FakeMympd.tiles[0]["name"], "Other")

    def test_tile_is_hub_icon_on_mympd_green(self):
        installer.install(self.endpoint, "http://a.test:18773/")
        ours = FakeMympd.tiles[-1]
        self.assertEqual((ours["ligature"], ours["bgcolor"], ours["color"], ours["image"]),
                         ("hub", "#28a745", "#ffffff", ""))

    def test_session_header_is_sent_when_set(self):
        with unittest.mock.patch.dict("os.environ", {"MYMPD_SESSION": "abc"}):
            installer.install(self.endpoint, "http://a.test:18773/")
        self.assertEqual(set(FakeMympd.sessions), {"abc"})

    def test_refusal_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "rejected"):
            installer.rpc(self.endpoint, "MYMPD_API_HOME_ICON_SAVE", {"name": "refuse"})

    def test_url_validation(self):
        self.assertTrue(installer.valid_http_url("http://192.168.1.80:18773/"))
        for bad in ("ftp://host/", "http://user@host/", "http://host/?a=1",
                    "javascript:alert(1)", "http://host/ x", "not a url"):
            self.assertFalse(installer.valid_http_url(bad), bad)


if __name__ == "__main__":
    unittest.main()
