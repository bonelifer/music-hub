"""Calls to the mympd-* bridges, which hold the logins and talk to each service.

music-hub never signs in to a service itself. With myMPD in use it calls
the same bridges myMPD does, so both share one login per service. Without
MPD/myMPD, compose.standalone.yaml runs music-hub's own copies instead.
"""

import json
import random
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

TIMEOUT = 30
# The largest JSON answer read from a bridge (a big library listing).
MAX_BODY = 32 * 1024 * 1024
NUMBER = re.compile(r"[0-9]{1,12}")
SOMAFM_ID = re.compile(r"[a-zA-Z0-9-]{1,64}")
TUNEIN_ID = re.compile(r"s[0-9]{1,12}")
# iBroadcast browse views and the parameters each may carry.
IBROADCAST_MODES = ("genre", "year", "tag", "playlist", "search", "recent",
                    "rated", "played")


class BridgeError(Exception):
    """A bridge could not be reached or turned the request down.

    status is the HTTP status to give the browser: 502 when the bridge or
    its service failed, 400 or 404 when the request itself was the problem.
    """

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


class Bridge:
    """One bridge's HTTP address and JSON calls."""

    # Seconds to wait for a bridge answer.
    timeout = TIMEOUT

    def __init__(self, label: str, url: str) -> None:
        self.label = label
        self.url = url

    def request(self, path: str, body=None, method: str = None):
        """Send a request and return the decoded JSON (None when empty).

        Bridge POST routes only accept local callers that send JSON and no
        Origin header; music-hub runs beside them, so it qualifies.
        """
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.url + path, data, headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_BODY + 1)
        except HTTPError as error:
            with error:
                status = 404 if error.code == 404 else \
                    400 if error.code == 400 else 502
                raise BridgeError(
                    f"{self.label} turned the request down "
                    f"(HTTP {error.code})", status) from None
        except (URLError, OSError) as error:
            raise BridgeError(
                f"Could not reach the {self.label} bridge at {self.url}: "
                f"{getattr(error, 'reason', error)}") from None
        if len(raw) > MAX_BODY:
            raise BridgeError(f"{self.label} sent too much data")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise BridgeError(f"{self.label} sent an unreadable answer") from None

    def open_stream(self, path: str, byte_range: str = ""):
        """Open an audio stream from the bridge; the caller closes it."""
        request = Request(self.url + path)
        if byte_range:
            request.add_header("Range", byte_range)
        try:
            return urlopen(request, timeout=self.timeout)
        except HTTPError as error:
            error.close()
            raise BridgeError(
                f"{self.label} could not open that track (HTTP {error.code})",
                404 if error.code == 404 else 502) from None
        except (URLError, OSError) as error:
            raise BridgeError(
                f"Could not reach the {self.label} bridge: "
                f"{getattr(error, 'reason', error)}") from None


def require(pattern, value, what: str) -> str:
    """Return value when it matches pattern; otherwise a 400 BridgeError."""
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise BridgeError(f"Invalid {what}", 400)
    return value


def search_text(value) -> str:
    """A search query: 1 to 100 characters after trimming."""
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100:
        raise BridgeError("Enter 1 to 100 characters to search for", 400)
    return value.strip()


# --- iBroadcast ---------------------------------------------------------


class IBroadcast(Bridge):
    """The mympd-ibroadcast bridge: the account's own uploaded library."""

    # The bridge reloads the whole library every five minutes, which can
    # take over 30 seconds for a large library.
    timeout = 90

    def __init__(self, url: str) -> None:
        super().__init__("iBroadcast", url)

    def stream_uri(self, track_id: str) -> str:
        """The URI MPD plays, identical to the one myMPD queues."""
        return f"{self.url}/stream/{track_id}"

    def artists(self) -> list:
        """Every artist, sorted by name."""
        return self.request("/artists")

    def albums(self, artist_id: str = "") -> list:
        """Every album, or one artist's albums."""
        if artist_id:
            return self.request("/albums/" + require(NUMBER, artist_id, "artist"))
        return self.request("/albums")

    def album_tracks(self, album_id: str) -> list:
        """One album's tracks in album order."""
        return self.request("/tracks/" + require(NUMBER, album_id, "album"))

    def browse(self, mode: str, value: str = "") -> list:
        """One of the bridge's browse views (genre, search, top rated...).

        Without a value, genre/year/tag/playlist list their groups; with
        one, they list that group's tracks.
        """
        if mode not in IBROADCAST_MODES:
            raise BridgeError("Invalid iBroadcast view", 400)
        if mode == "search":
            value = search_text(value)
        elif not isinstance(value, str) or len(value) > 100:
            raise BridgeError("Invalid iBroadcast value", 400)
        return self.request("/browse?" + urlencode({"mode": mode, "value": value}))

    def random_tracks(self, count: int) -> list:
        """count distinct random tracks (1 to 100)."""
        if not isinstance(count, int) or not 1 <= count <= 100:
            raise BridgeError("Pick from 1 to 100 songs", 400)
        return self.request(f"/browse?mode=random&value={count}")

    def save_playlist(self, name: str, ids: list) -> None:
        """Save tracks, in order, as a private iBroadcast playlist."""
        result = self.request("/playlists", {"name": name, "ids": ids})
        if not isinstance(result, dict) or result.get("saved") is not True:
            raise BridgeError("iBroadcast did not confirm the save")

    def track_ids(self, kind: str, value: str) -> list:
        """Track IDs for a play action: a track, an album, or a group.

        Albums and playlists keep their order; genres, years and tags are
        shuffled, as the myMPD screen does.
        """
        if kind == "track":
            return [require(NUMBER, value, "track")]
        if kind == "album":
            ids = [item["id"] for item in self.album_tracks(value)]
        elif kind in ("playlist", "genre", "year", "tag"):
            ids = [item["id"] for item in self.browse(kind, value)]
            if kind != "playlist":
                random.shuffle(ids)
        else:
            raise BridgeError("Invalid iBroadcast selection", 400)
        return [key for key in map(str, ids) if NUMBER.fullmatch(key)]


# --- Radio --------------------------------------------------------------


class Radio(Bridge):
    """Shared shape of the radio bridges: favorites, resolving, playing."""

    station_id = NUMBER
    # Fragment the myMPD display scripts read; None when none is used.
    fragment = None

    def favorites(self) -> list:
        """Saved stations as {id, name, image} rows."""
        return [self.row(item) for item in self.request("/stations")]

    @staticmethod
    def row(item: dict) -> dict:
        """Keep the fields the page shows, with one name for the image."""
        image = item.get("image") or item.get("artUrl") or item.get("logo") or ""
        return {"id": str(item.get("id", "")), "name": item.get("name", ""),
                "image": image if image.startswith("https://") else ""}

    def resolve(self, station: str) -> dict:
        """A station's playable stream: {url, name, artFile}."""
        require(self.station_id, station, "station")
        result = self.request("/stations/" + station)
        url = result.get("url") if isinstance(result, dict) else None
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise BridgeError(f"{self.label} sent no playable stream")
        return {"url": url, "name": result.get("name", ""),
                "artFile": result.get("artFile") or ""}

    def mpd_uri(self, station: str, stream: dict) -> str:
        """The URI MPD plays, tagged the same way the myMPD script tags it.

        myMPD's display script reads the fragment to show the station name
        and logo, so a station started here looks the same in myMPD.
        """
        url = stream["url"].split("#", 1)[0]
        if not self.fragment:
            return url
        return (f"{url}#{self.fragment}={station}"
                f"&name={quote(stream['name'], safe='')}"
                f"&art={quote(stream['artFile'], safe='')}")


class SomaFM(Radio):
    """The mympd-somafm bridge: every SomaFM channel, no account needed."""

    station_id = SOMAFM_ID

    def __init__(self, url: str) -> None:
        super().__init__("SomaFM", url)

    def recent(self, station: str) -> list:
        """The songs a channel played most recently."""
        return self.request("/recent/" + require(SOMAFM_ID, station, "station"))


class TuneIn(Radio):
    """The mympd-tunein bridge: the account's TuneIn favorites.

    The bridge only plays stations saved as favorites, so a search result
    is saved first and then appears in the favorites list.
    """

    station_id = TUNEIN_ID
    fragment = "mympd-tunein"

    def __init__(self, url: str) -> None:
        super().__init__("TuneIn", url)

    def search(self, query: str) -> list:
        """Stations matching a name or call sign."""
        return [self.row(item) for item in
                self.request("/search?" + urlencode({"q": search_text(query)}))]

    def change_favorite(self, action: str, station: str) -> None:
        """Add or remove a favorite."""
        if action not in ("add", "remove"):
            raise BridgeError("Invalid favorite action", 400)
        require(TUNEIN_ID, station, "station")
        self.request(f"/favorites/{action}/{station}", {})


class IHeart(Radio):
    """The mympd-iheart bridge: favorites, live stations, and podcasts."""

    fragment = "mympd-iheart"

    def __init__(self, url: str) -> None:
        super().__init__("iHeartRadio", url)

    def search(self, query: str) -> list:
        """Live stations matching a name or call sign."""
        return [self.row(item) for item in
                self.request("/search?" + urlencode({"q": search_text(query)}))]

    def save_favorite(self, station: str) -> None:
        """Follow a live station so it shows in favorites."""
        require(NUMBER, station, "station")
        self.request(f"/favorites/{station}", {})

    def podcasts(self, query: str) -> list:
        """Podcasts matching a search."""
        return self.request("/podcasts?" + urlencode({"q": search_text(query)}))

    def episodes(self, podcast: str) -> list:
        """A podcast's episodes, newest first."""
        return self.request("/podcasts/" + require(NUMBER, podcast, "podcast"))

    def episode(self, episode: str) -> dict:
        """One episode's playable URL and name."""
        result = self.request("/episodes/" + require(NUMBER, episode, "episode"))
        url = result.get("url") if isinstance(result, dict) else None
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise BridgeError("iHeartRadio sent no playable episode")
        return {"url": url, "name": result.get("name", "")}


CLASSES = {"ibroadcast": IBroadcast, "tunein": TuneIn, "iheart": IHeart,
           "somafm": SomaFM}


def build(config) -> dict:
    """A bridge client for every enabled service, keyed by service name."""
    return {service.name: CLASSES[service.name](service.bridge_url)
            for service in config.enabled_services()}
