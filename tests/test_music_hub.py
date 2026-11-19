"""Offline tests for music-hub: config, MPD client, bridges and the web API.

A fake bridge (an HTTP server) and a fake MPD (a socket server) run in
threads, so no real service, MPD or network access is needed.
"""

import json
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import music_hub  # noqa: E402
import web  # noqa: E402
from mpd import Mpd, MpdError, quote  # noqa: E402


class FakeMpd:
    """A tiny MPD that records commands and keeps stored playlists."""

    def __init__(self) -> None:
        self.commands = []
        self.playlists = {"taken": []}
        self.next_id = 1
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.client, args=(conn,), daemon=True).start()

    def answer(self, command: str, out: list) -> str:
        """Run one command; return an ACK line on failure, else ''."""
        self.commands.append(command)
        name, _, rest = command.partition(" ")
        args = [part.replace('\\"', '"') for part in rest[1:-1].split('" "')] \
            if rest.startswith('"') else rest.split()
        if name == "addid":
            out.append(f"Id: {self.next_id}")
            self.next_id += 1
        elif name == "playlistadd":
            self.playlists.setdefault(args[0], []).append(args[1])
        elif name == "rename":
            if args[1] in self.playlists:
                return "ACK [56@0] {rename} Playlist exists already"
            self.playlists[args[1]] = self.playlists.pop(args[0])
        elif name == "rm":
            self.playlists.pop(args[0], None)
        elif name == "status":
            out.append("state: play")
        elif name == "currentsong":
            out.extend(["file: http://x/stream/1", "Title: Song", "Artist: Band"])
        elif name not in ("play", "playid", "pause", "stop", "next", "previous"):
            return f"ACK [5@0] {{{name}}} unknown command"
        return ""

    def client(self, conn) -> None:
        with conn:
            stream = conn.makefile("rwb")
            stream.write(b"OK MPD 0.24.0\n")
            stream.flush()
            batch = None
            for raw in stream:
                line = raw.decode().rstrip("\n")
                if line == "command_list_begin":
                    batch = []
                    continue
                commands = [line] if batch is None else None
                if line == "command_list_end":
                    commands, batch = batch, None
                elif batch is not None:
                    batch.append(line)
                    continue
                out = []
                error = ""
                for command in commands:
                    error = self.answer(command, out)
                    if error:
                        break
                for item in out:
                    stream.write(item.encode() + b"\n")
                stream.write((error or "OK").encode() + b"\n")
                stream.flush()

    def close(self) -> None:
        self.sock.close()


class FakeBridge(BaseHTTPRequestHandler):
    """Answers like the mympd-* bridges for the routes music-hub uses."""

    posts = []
    fail_playlists = False

    def log_message(self, *args) -> None:
        pass

    def send(self, data, status=200) -> None:
        body = json.dumps(data).encode() if data is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        routes = {
            "/artists": [{"id": "1", "name": "Band"}],
            "/albums/1": [{"id": "10", "name": "Record"}],
            "/tracks/10": [{"id": "100", "name": "One"}, {"id": "101", "name": "Two"}],
            "/browse?mode=random&value=2": [{"id": "100", "name": "One"},
                                            {"id": "101", "name": "Two"}],
            "/browse?mode=genre&value=Rock": [{"id": "100", "name": "One"}],
            "/stations": [{"id": "s1", "name": "Station", "artUrl": "https://a/x.png"},
                          {"id": "s2", "name": "Plain", "logo": "http://insecure/x.png"}],
            "/stations/s1": {"id": "s1", "name": "Station", "url": "https://r/s1#old",
                             "artFile": "s1.png"},
            "/stations/s9": {"name": "Broken"},
        }
        if self.path == "/stream/100":
            body = b"0123456789"
            if self.headers.get("Range") == "bytes=5-":
                self.send_response(206)
                self.send_header("Content-Range", "bytes 5-9/10")
                body = body[5:]
            else:
                self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in routes:
            self.send(routes[self.path])
        else:
            self.send({"error": "missing"}, 404)

    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size))
        FakeBridge.posts.append((self.path, body, self.headers.get("Origin")))
        if self.path == "/playlists":
            if FakeBridge.fail_playlists:
                self.send({"error": "no"}, 502)
            else:
                self.send({"saved": True})
        else:
            self.send({"ok": True})


def write_config(folder: Path, text: str) -> Path:
    path = folder / "music-hub.cfg"
    path.write_text(text, encoding="utf-8")
    return path


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())

    def test_example_file_is_valid(self) -> None:
        example = Path(__file__).resolve().parent.parent / "music-hub.cfg.example"
        settings = config.load(example)
        self.assertEqual(settings.port, 18773)
        self.assertEqual([s.name for s in settings.enabled_services()],
                         ["ibroadcast", "tunein", "iheart", "somafm"])
        self.assertEqual(settings.services["ibroadcast"].bridge_url,
                         "http://127.0.0.1:18769")

    def test_missing_file_names_the_template(self) -> None:
        with self.assertRaisesRegex(config.ConfigError, "music-hub.cfg.example"):
            config.load(self.folder / "nope.cfg")

    def test_bad_values_name_the_setting(self) -> None:
        cases = {
            "[server]\nport = 99999\n": r"\[server\] port",
            "[server]\nlisten = example.com\n": r"\[server\] listen",
            "[playback]\nmpd_enabled = maybe\n": r"\[playback\] mpd_enabled",
            "[playback]\nmpd_enabled = no\nbrowser_enabled = no\n": r"\[playback\]",
            "[tunein]\nbridge_url = https://x:1\n": r"\[tunein\] bridge_url",
            "[tunein]\nbridge_url = http://x\n": r"\[tunein\] bridge_url",
            "[tunein]\nbridge_url = http://x:1/path\n": r"\[tunein\] bridge_url",
            "[ibroadcast]\nrandom_playlist_size = 0\n": "random_playlist_size",
            "[ibroadcast]\nplaylist_destination = cloud\n": "playlist_destination",
        }
        for text, pattern in cases.items():
            with self.subTest(text=text):
                with self.assertRaisesRegex(config.ConfigError, pattern):
                    config.load(write_config(self.folder, text))

    def test_without_mpd_playlists_go_to_ibroadcast(self) -> None:
        settings = config.load(write_config(
            self.folder, "[playback]\nmpd_enabled = false\n"
            "[ibroadcast]\nplaylist_destination = both\n"))
        self.assertEqual(settings.playlist_destination, "ibroadcast")

    def test_allowed_hosts_always_include_local_names(self) -> None:
        settings = config.load(write_config(
            self.folder, "[server]\nlisten = 192.168.1.5\nallowed_hosts = Music.LAN\n"))
        self.assertEqual(settings.allowed_hosts,
                         ("music.lan", "127.0.0.1", "localhost", "[::1]", "192.168.1.5"))

    def test_disabled_service_is_left_out(self) -> None:
        settings = config.load(write_config(self.folder, "[somafm]\nenabled = false\n"))
        self.assertNotIn("somafm", [s.name for s in settings.enabled_services()])


class MpdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeMpd()
        self.mpd = Mpd("127.0.0.1", self.fake.port)

    def tearDown(self) -> None:
        self.fake.close()

    def test_quote_escapes_and_rejects_line_breaks(self) -> None:
        self.assertEqual(quote('a "b" \\c'), '"a \\"b\\" \\\\c"')
        with self.assertRaises(MpdError):
            quote("a\nb")

    def test_queue_and_play_first(self) -> None:
        self.mpd.queue(["http://x/1", "http://x/2"], play=True)
        self.assertIn('addid "http://x/1"', self.fake.commands)
        self.assertEqual(self.fake.commands[-1], "playid 1")

    def test_queue_without_play(self) -> None:
        self.mpd.queue(["http://x/1"], play=False)
        self.assertFalse(any(c.startswith("playid") for c in self.fake.commands))

    def test_save_playlist_never_replaces_one(self) -> None:
        self.mpd.save_playlist("new", ["http://x/1"])
        self.assertEqual(self.fake.playlists["new"], ["http://x/1"])
        with self.assertRaisesRegex(MpdError, "exists"):
            self.mpd.save_playlist("taken", ["http://x/2"])
        self.assertEqual(self.fake.playlists["taken"], [])
        self.assertEqual(sorted(self.fake.playlists), ["new", "taken"])

    def test_status_and_controls(self) -> None:
        self.assertEqual(self.mpd.status()["title"], "Song")
        self.mpd.control("pause")
        self.assertEqual(self.fake.commands[-1], "pause 1")
        with self.assertRaises(MpdError):
            self.mpd.control("clear")

    def test_unreachable_mpd_is_an_mpd_error(self) -> None:
        # A port that was bound but never listened on refuses connections.
        unused = socket.socket()
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
        unused.close()
        with self.assertRaisesRegex(MpdError, "Could not reach"):
            Mpd("127.0.0.1", port).status()


class HubTestCase(unittest.TestCase):
    """Starts a fake bridge, a fake MPD and music-hub itself."""

    extra_config = ""

    def setUp(self) -> None:
        FakeBridge.posts = []
        FakeBridge.fail_playlists = False
        self.bridge = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
        threading.Thread(target=self.bridge.serve_forever, daemon=True).start()
        self.mpd = FakeMpd()
        folder = Path(tempfile.mkdtemp())
        bridge = f"http://127.0.0.1:{self.bridge.server_address[1]}"
        text = (f"[server]\nport = 1\n[mpd]\nport = {self.mpd.port}\n"
                + "".join(f"[{name}]\nbridge_url = {bridge}\n" for name in config.SERVICES)
                + self.extra_config)
        settings = config.load(write_config(folder, text))
        settings.port = 0
        self.hub = web.HubServer(settings, folder / "state")
        threading.Thread(target=self.hub.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.hub.server_address[1]}"

    def tearDown(self) -> None:
        self.hub.shutdown()
        self.hub.server_close()
        self.bridge.shutdown()
        self.bridge.server_close()
        self.mpd.close()

    def get(self, path, headers=None):
        try:
            with urlopen(Request(self.base + path, headers=headers or {}), timeout=10) as r:
                return r.status, r.read(), r.headers
        except HTTPError as error:
            with error:
                return error.code, error.read(), error.headers

    def post(self, path, body, headers=None):
        sent = {"Content-Type": "application/json", "X-Music-Hub": "1", **(headers or {})}
        request = Request(self.base + path, json.dumps(body).encode(), sent)
        try:
            with urlopen(request, timeout=10) as r:
                return r.status, json.loads(r.read())
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read())


class WebTests(HubTestCase):
    def test_page_carries_theme_and_security_headers(self) -> None:
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b'<html lang="en">', body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.post("/api/theme", {"theme": "dark"})[0], 200)
        self.assertIn(b'<html lang="en" data-theme="dark">', self.get("/")[1])

    def test_only_listed_static_files_are_served(self) -> None:
        self.assertEqual(self.get("/static/app.js")[0], 200)
        self.assertEqual(self.get("/static/../web.py")[0], 404)
        self.assertEqual(self.get("/static/index.html")[0], 404)

    def test_unknown_host_is_refused(self) -> None:
        self.assertEqual(self.get("/api/config", {"Host": "evil.example"})[0], 403)

    def test_posts_need_header_json_and_same_origin(self) -> None:
        self.assertEqual(self.post("/api/theme", {"theme": "dark"},
                                   {"X-Music-Hub": "0"})[0], 403)
        self.assertEqual(self.post("/api/theme", {"theme": "dark"},
                                   {"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.post("/api/theme", {"theme": "dark"},
                                   {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.post("/api/theme", {"theme": "purple"})[0], 400)

    def test_config_lists_enabled_services(self) -> None:
        data = json.loads(self.get("/api/config")[1])
        self.assertEqual([s["name"] for s in data["services"]],
                         ["ibroadcast", "tunein", "iheart", "somafm"])
        self.assertEqual(data["playback"], {"mpd": True, "browser": True})

    def test_ibroadcast_browse_and_queue(self) -> None:
        self.assertEqual(json.loads(self.get("/api/ibroadcast/albums?artist=1")[1]),
                         [{"id": "10", "name": "Record"}])
        self.assertEqual(self.get("/api/ibroadcast/albums?artist=x")[0], 400)
        self.assertEqual(self.get("/api/ibroadcast/browse?mode=random&value=2")[0], 400)
        status, data = self.post("/api/ibroadcast/queue", {"kind": "album", "value": "10"})
        self.assertEqual((status, data), (200, {"queued": 2}))
        bridge = f"http://127.0.0.1:{self.bridge.server_address[1]}"
        self.assertIn(f'addid "{bridge}/stream/100"', self.mpd.commands)

    def test_ibroadcast_stream_relays_ranges(self) -> None:
        status, body, headers = self.get("/api/ibroadcast/stream/100", {"Range": "bytes=5-"})
        self.assertEqual((status, body), (206, b"56789"))
        self.assertEqual(headers["Content-Range"], "bytes 5-9/10")

    def test_random_playlist_to_both(self) -> None:
        status, data = self.post("/api/ibroadcast/random",
                                 {"name": "Mix", "count": 2, "destination": "both"})
        self.assertEqual((status, data), (200, {"saved": 2, "destination": "both"}))
        self.assertEqual(len(self.mpd.playlists["Mix"]), 2)
        path, body, origin = FakeBridge.posts[-1]
        self.assertEqual((path, body, origin), ("/playlists", {"name": "Mix",
                                                               "ids": ["100", "101"]}, None))

    def test_random_playlist_reports_partial_save(self) -> None:
        FakeBridge.fail_playlists = True
        status, data = self.post("/api/ibroadcast/random",
                                 {"name": "Mix", "count": 2, "destination": "both"})
        self.assertEqual(status, 502)
        self.assertIn("Saved to MPD", data["error"])

    def test_random_playlist_refuses_taken_name_and_slashes(self) -> None:
        self.assertEqual(self.post("/api/ibroadcast/random", {
            "name": "taken", "count": 2, "destination": "mpd"})[0], 409)
        self.assertEqual(self.post("/api/ibroadcast/random", {
            "name": "a/b", "count": 2, "destination": "mpd"})[0], 400)

    def test_station_play_tags_uri_like_mympd(self) -> None:
        status, data = self.post("/api/tunein/play", {"id": "s1"})
        self.assertEqual((status, data), (200, {"playing": "Station"}))
        self.assertIn('addid "https://r/s1#mympd-tunein=s1&name=Station&art=s1.png"',
                      self.mpd.commands)

    def test_favorites_keep_only_https_images(self) -> None:
        rows = json.loads(self.get("/api/tunein/favorites")[1])
        self.assertEqual(rows[0]["image"], "https://a/x.png")
        self.assertEqual(rows[1]["image"], "")

    def test_station_without_stream_is_an_error(self) -> None:
        self.assertEqual(self.get("/api/tunein/resolve/s9")[0], 502)
        self.assertEqual(self.get("/api/tunein/resolve/../x")[0], 404)

    def test_favorite_change_reaches_bridge_without_origin(self) -> None:
        self.assertEqual(self.post("/api/tunein/favorite",
                                   {"action": "remove", "id": "s1"})[0], 200)
        self.assertEqual(FakeBridge.posts[-1], ("/favorites/remove/s1", {}, None))

    def test_mpd_status_and_control(self) -> None:
        self.assertEqual(json.loads(self.get("/api/mpd/status")[1])["state"], "play")
        self.assertEqual(self.post("/api/mpd/control", {"action": "next"})[0], 200)


class StandaloneTests(HubTestCase):
    """MPD turned off: no MPD routes, playlists only to iBroadcast."""

    extra_config = "[playback]\nmpd_enabled = false\n"

    def test_mpd_routes_are_off(self) -> None:
        self.assertEqual(self.get("/api/mpd/status")[0], 404)
        self.assertEqual(self.post("/api/tunein/play", {"id": "s1"})[0], 404)
        self.assertEqual(self.post("/api/ibroadcast/random", {
            "name": "Mix", "count": 2, "destination": "mpd"})[0], 400)
        self.assertEqual(self.post("/api/ibroadcast/random", {
            "name": "Mix", "count": 2, "destination": "ibroadcast"})[0], 200)
        self.assertEqual(self.mpd.commands, [])


class CliTests(unittest.TestCase):
    def test_no_arguments_prints_help(self) -> None:
        self.assertEqual(music_hub.main([]), 0)

    def test_check_config_reports_errors(self) -> None:
        folder = Path(tempfile.mkdtemp())
        bad = write_config(folder, "[server]\nport = nope\n")
        self.assertEqual(music_hub.main(["check-config", "--config", str(bad)]), 1)


if __name__ == "__main__":
    unittest.main()
