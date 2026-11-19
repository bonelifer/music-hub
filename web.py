"""The music-hub HTTP server: the page, its files, and a JSON API.

The page calls /api/... routes, which call the bridges and MPD. Bridges
stay on 127.0.0.1; only this server talks to them.

Requests are checked before anything runs:
- the Host header must be one of [server] allowed_hosts, which stops
  DNS-rebinding pages from reaching the API through the browser;
- every POST must be JSON, carry the X-Music-Hub header, and come from
  this page's own origin, so another site cannot send one.
"""

import json
import os
import re
import shutil
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import bridges
import config as config_module
from mpd import Mpd, MpdError

STATIC = Path(__file__).resolve().parent / "static"
# Files the page loads, with their content types.
STATIC_FILES = {
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}
THEMES = ("auto", "light", "dark")
MAX_POST = 16 * 1024
# Stream URLs come from many radio hosts, over http and https.
CSP = ("default-src 'self'; img-src 'self' https:; media-src 'self' http: https:; "
       "style-src 'self'; script-src 'self'; connect-src 'self'; "
       "form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
PLAYLIST_NAME = re.compile(r"[^\x00-\x1f/\\]{1,100}")


class ApiError(Exception):
    """A request the API answers with an error message and status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class State:
    """Small saved choices (the theme) in state/settings.json.

    Written atomically so an interrupted save never leaves a broken file.
    """

    def __init__(self, folder: Path) -> None:
        self.path = folder / "settings.json"

    def read(self) -> dict:
        """The saved choices, or an empty dict when none are saved yet."""
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def theme(self) -> str:
        """"auto", "light" or "dark"."""
        theme = self.read().get("theme")
        return theme if theme in THEMES else "auto"

    def set_theme(self, theme: str) -> None:
        """Save the theme choice."""
        data = self.read()
        data["theme"] = theme
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.path.parent,
                                             prefix=".settings-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as output:
                json.dump(data, output)
            os.replace(temporary, self.path)
        except OSError:
            Path(temporary).unlink(missing_ok=True)
            raise


class HubServer(ThreadingHTTPServer):
    """Holds the config, bridge clients, MPD client and saved state."""

    daemon_threads = True

    def __init__(self, settings, state_dir: Path) -> None:
        super().__init__((settings.listen, settings.port), Handler)
        self.settings = settings
        self.bridges = bridges.build(settings)
        self.mpd = Mpd(settings.mpd_host, settings.mpd_port,
                       settings.mpd_password) if settings.mpd_enabled else None
        self.state = State(state_dir)


class Handler(BaseHTTPRequestHandler):
    """Routes page, static and API requests."""

    server_version = "music-hub"
    sys_version = ""

    # --- request entry points ------------------------------------------

    def do_GET(self) -> None:
        """Serve the page, a static file, an audio stream, or API data."""
        if not self.host_allowed():
            return
        target = urlsplit(self.path)
        path, query = target.path, parse_qs(target.query)
        if path == "/":
            self.send_page()
        elif path.startswith("/static/"):
            self.send_static(path.removeprefix("/static/"))
        elif match := re.fullmatch(r"/api/ibroadcast/stream/([0-9]{1,12})", path):
            self.send_ibroadcast_stream(match[1])
        elif path.startswith("/api/"):
            self.answer(lambda: self.api_get(path, query))
        else:
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        """Run an API action after checking the request is from this page."""
        if not self.host_allowed():
            return
        path = urlsplit(self.path).path
        if not path.startswith("/api/"):
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        self.answer(lambda: self.api_post(path, self.read_body()))

    def log_request(self, code="-", size="-") -> None:
        """Log only failed requests, so a healthy server stays quiet."""
        try:
            failed = int(code) >= 400
        except (TypeError, ValueError):
            failed = True
        if failed:
            super().log_request(code, size)

    # --- checks --------------------------------------------------------

    def host_allowed(self) -> bool:
        """Refuse requests whose Host header is not an allowed name."""
        host = (self.headers.get("Host") or "").lower()
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if name in self.server.settings.allowed_hosts:
            return True
        self.send_json({"error": "Host not allowed; add it to [server] "
                        "allowed_hosts"}, HTTPStatus.FORBIDDEN)
        return False

    def read_body(self) -> dict:
        """Read a JSON POST body after the same-page checks."""
        if self.headers.get("X-Music-Hub") != "1":
            raise ApiError("Missing X-Music-Hub header", 403)
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() != \
                (self.headers.get("Host") or "").lower():
            raise ApiError("Requests must come from this page", 403)
        if self.headers.get_content_type() != "application/json":
            raise ApiError("Send JSON", 415)
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = -1
        if not 0 < size <= MAX_POST:
            raise ApiError("Invalid request body")
        try:
            body = json.loads(self.rfile.read(size))
        except ValueError:
            raise ApiError("Invalid JSON") from None
        if not isinstance(body, dict):
            raise ApiError("Invalid request body")
        return body

    # --- answering -----------------------------------------------------

    def answer(self, action) -> None:
        """Run an API action and send its result or a readable error."""
        try:
            result = action()
        except ApiError as error:
            self.send_json({"error": str(error)}, error.status)
        except bridges.BridgeError as error:
            self.send_json({"error": str(error)}, error.status)
        except MpdError as error:
            self.send_json({"error": f"MPD: {error}"}, HTTPStatus.BAD_GATEWAY)
        else:
            self.send_json(result if result is not None else {"ok": True})

    def send_json(self, data, status: int = HTTPStatus.OK) -> None:
        """Send a JSON response."""
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_page(self) -> None:
        """Send index.html with the saved theme already applied.

        Setting data-theme on the server avoids a flash of the wrong
        colors before the script runs. "auto" leaves it off, so the
        system's light/dark setting decides.
        """
        theme = self.server.state.theme()
        attribute = "" if theme == "auto" else f' data-theme="{theme}"'
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        body = page.replace("<html lang=\"en\">",
                            f"<html lang=\"en\"{attribute}>", 1).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        # Needed for playback, not only privacy: SomaFM refuses streams
        # requested with another site's Referer (403), and the browser's
        # player sends one unless the page says not to.
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, name: str) -> None:
        """Send one of the page's own files (no other paths are served)."""
        if name not in STATIC_FILES:
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        body = (STATIC / name).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", STATIC_FILES[name])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_ibroadcast_stream(self, track_id: str) -> None:
        """Pass an iBroadcast track through to the browser's player.

        The bridge only listens on 127.0.0.1, so a browser on another
        device cannot reach it; this relays the audio, including seeking.
        """
        bridge = self.server.bridges.get("ibroadcast")
        if bridge is None or not self.server.settings.browser_enabled:
            self.send_json({"error": "Browser playback of iBroadcast is off"},
                           HTTPStatus.NOT_FOUND)
            return
        byte_range = self.headers.get("Range", "")
        if not re.fullmatch(r"bytes=[0-9]+-[0-9]*", byte_range):
            byte_range = ""
        try:
            response = bridge.open_stream("/stream/" + track_id, byte_range)
        except bridges.BridgeError as error:
            self.send_json({"error": str(error)}, error.status)
            return
        with response:
            self.send_response(response.status)
            for header in ("Content-Type", "Content-Length", "Content-Range",
                           "Accept-Ranges"):
                if value := response.headers.get(header):
                    self.send_header(header, value)
            self.end_headers()
            try:
                shutil.copyfileobj(response, self.wfile, length=65536)
            except OSError:
                self.close_connection = True  # The listener went away.

    # --- API -----------------------------------------------------------

    def service(self, name: str):
        """The bridge client for an enabled service, or a 404."""
        bridge = self.server.bridges.get(name)
        if bridge is None:
            raise ApiError(f"{name} is turned off in music-hub.cfg", 404)
        return bridge

    def player(self) -> Mpd:
        """The MPD client, or an error when MPD playback is turned off."""
        if self.server.mpd is None:
            raise ApiError("MPD playback is turned off in music-hub.cfg", 404)
        return self.server.mpd

    def api_get(self, path: str, query: dict):
        """Answer a GET API route."""
        def arg(key: str) -> str:
            return query.get(key, [""])[0]

        settings = self.server.settings
        if path == "/api/config":
            return {
                "services": [{"name": item.name, "label": item.label}
                             for item in settings.enabled_services()],
                "playback": {"mpd": settings.mpd_enabled,
                             "browser": settings.browser_enabled},
                "playlist": {"size": settings.random_playlist_size,
                             "destination": settings.playlist_destination},
                "theme": self.server.state.theme(),
            }
        if path == "/api/mpd/status":
            return self.player().status()

        parts = path.removeprefix("/api/").split("/")
        name, action, rest = parts[0], parts[1] if len(parts) > 1 else "", parts[2:]
        bridge = self.service(name)
        identifier = rest[0] if len(rest) == 1 else ""
        if name == "ibroadcast":
            if action == "artists" and not rest:
                return bridge.artists()
            if action == "albums" and not rest:
                return bridge.albums(arg("artist"))
            if action == "tracks" and not rest:
                return bridge.album_tracks(arg("album"))
            if action == "browse" and not rest:
                return bridge.browse(arg("mode"), arg("value"))
        else:
            if action == "favorites" and not rest:
                return bridge.favorites()
            if action == "resolve" and identifier:
                return bridge.resolve(identifier)
            if action == "search" and not rest and name in ("tunein", "iheart"):
                return bridge.search(arg("q"))
            if name == "somafm" and action == "recent" and identifier:
                return bridge.recent(identifier)
            if name == "iheart" and action == "podcasts" and not rest:
                return bridge.podcasts(arg("q"))
            if name == "iheart" and action == "episodes" and identifier:
                return bridge.episodes(identifier)
            if name == "iheart" and action == "episode" and identifier:
                return bridge.episode(identifier)
        raise ApiError("Not found", 404)

    def api_post(self, path: str, body: dict):
        """Answer a POST API route."""
        if path == "/api/theme":
            theme = body.get("theme")
            if theme not in THEMES:
                raise ApiError("Theme must be auto, light, or dark")
            self.server.state.set_theme(theme)
            return {"theme": theme}
        if path == "/api/mpd/control":
            self.player().control(str(body.get("action", "")))
            return None

        parts = path.removeprefix("/api/").split("/")
        if len(parts) != 2:
            raise ApiError("Not found", 404)
        name, action = parts
        bridge = self.service(name)
        if name == "ibroadcast" and action == "queue":
            ids = bridge.track_ids(str(body.get("kind", "")),
                                   str(body.get("value", "")))
            if not ids:
                raise ApiError("No iBroadcast tracks found", 404)
            self.player().queue([bridge.stream_uri(key) for key in ids],
                                play=body.get("play") is not False)
            return {"queued": len(ids)}
        if name == "ibroadcast" and action == "random":
            return self.random_playlist(bridge, body)
        if name != "ibroadcast" and action == "play":
            station = str(body.get("id", ""))
            stream = bridge.resolve(station)
            self.player().queue([bridge.mpd_uri(station, stream)], play=True)
            return {"playing": stream["name"]}
        if name == "iheart" and action == "play-episode":
            episode = bridge.episode(str(body.get("id", "")))
            self.player().queue([episode["url"]], play=True)
            return {"playing": episode["name"]}
        if name == "tunein" and action == "favorite":
            bridge.change_favorite(str(body.get("action", "")),
                                   str(body.get("id", "")))
            return None
        if name == "iheart" and action == "favorite":
            bridge.save_favorite(str(body.get("id", "")))
            return None
        raise ApiError("Not found", 404)

    def random_playlist(self, bridge, body: dict) -> dict:
        """Save random iBroadcast tracks to MPD, iBroadcast, or both.

        Both destinations get the same tracks in the same order. MPD is
        saved first; when the iBroadcast save then fails, the answer says
        the MPD copy exists, so retrying does not make a duplicate blindly.
        """
        settings = self.server.settings
        name = body.get("name")
        name = name.strip() if isinstance(name, str) else ""
        if not PLAYLIST_NAME.fullmatch(name) or name in (".", ".."):
            raise ApiError("Enter a playlist name of up to 100 characters, "
                           "without slashes")
        count = body.get("count")
        destination = body.get("destination")
        if destination not in config_module.DESTINATIONS:
            raise ApiError("Destination must be mpd, ibroadcast, or both")
        if destination != "ibroadcast" and not settings.mpd_enabled:
            raise ApiError("MPD playback is turned off; save to iBroadcast")
        ids = [str(item["id"]) for item in bridge.random_tracks(count)]
        if not ids:
            raise ApiError("Could not load random iBroadcast tracks", 502)
        if destination != "ibroadcast":
            try:
                self.player().save_playlist(
                    name, [bridge.stream_uri(key) for key in ids])
            except MpdError as error:
                raise ApiError(f"Could not create the MPD playlist ({error}). "
                               "Check whether the name is already used.", 409) \
                    from None
        if destination != "mpd":
            try:
                bridge.save_playlist(name, ids)
            except bridges.BridgeError:
                if destination == "both":
                    raise ApiError("Saved to MPD. iBroadcast did not confirm the "
                                   "save; check iBroadcast before retrying.", 502) \
                        from None
                raise ApiError("iBroadcast did not confirm the save. Check the "
                               "library and write permission before retrying.",
                               502) from None
        return {"saved": len(ids), "destination": destination}


def serve(config_path: Path, state_dir: Path) -> None:
    """Load the config and serve until stopped."""
    settings = config_module.load(config_path)
    with HubServer(settings, state_dir) as server:
        print(f"music-hub listening on http://{settings.listen}:{settings.port}/",
              flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
