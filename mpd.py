"""A small MPD protocol client: queue tracks, save playlists, and control playback.

Each call opens its own short connection, so a restarted MPD never leaves
music-hub holding a dead socket.
"""

import secrets
import socket

TIMEOUT = 10
# Playback controls the web UI may send, mapped to MPD commands.
CONTROLS = {
    "play": "play",
    "pause": "pause 1",
    "resume": "pause 0",
    "stop": "stop",
    "next": "next",
    "previous": "previous",
}


class MpdError(Exception):
    """MPD could not be reached or refused a command."""


def quote(value: str) -> str:
    """Quote one MPD protocol argument; reject line breaks and NULs."""
    if any(character in value for character in "\r\n\0"):
        raise MpdError("argument contains a line break")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Mpd:
    """Talks to one MPD server."""

    def __init__(self, host: str, port: int, password: str = "") -> None:
        self.host = host
        self.port = port
        self.password = password

    def run(self, commands: list) -> list:
        """Send commands in one command list; return every key/value pair.

        The whole list fails if any command fails, and MPD stops at the
        first failing command. Raises MpdError with MPD's own message.
        """
        if self.password:
            commands = ["password " + quote(self.password), *commands]
        if len(commands) == 1:
            script = commands[0] + "\n"
        else:
            script = "command_list_begin\n" + "\n".join(commands) \
                + "\ncommand_list_end\n"
        try:
            with socket.create_connection((self.host, self.port),
                                          timeout=TIMEOUT) as conn:
                stream = conn.makefile("rwb")
                greeting = stream.readline().decode("utf-8", "replace")
                if not greeting.startswith("OK MPD"):
                    raise MpdError("MPD sent an unexpected greeting")
                stream.write(script.encode("utf-8"))
                stream.flush()
                pairs = []
                while True:
                    line = stream.readline().decode("utf-8", "replace")
                    if not line:
                        raise MpdError("MPD closed the connection")
                    line = line.rstrip("\n")
                    if line == "OK":
                        return pairs
                    if line.startswith("ACK"):
                        # "ACK [50@0] {rename} playlist already exists"
                        raise MpdError(line.split("} ", 1)[-1])
                    key, _, value = line.partition(": ")
                    pairs.append((key, value))
        except OSError as error:
            raise MpdError(f"Could not reach MPD: {error}") from None

    def queue(self, uris: list, play: bool) -> None:
        """Append URIs to the queue; when play is true, start the first one."""
        if not uris:
            raise MpdError("nothing to queue")
        commands = ["addid " + quote(uri) for uri in uris]
        pairs = self.run(commands)
        if play:
            first = next(value for key, value in pairs if key == "Id")
            self.run(["playid " + first])

    def save_playlist(self, name: str, uris: list) -> None:
        """Save URIs as a new stored playlist without replacing one.

        The tracks go into a temporary playlist first, which is then
        renamed. MPD refuses the rename when the name is taken, so an
        existing playlist is never overwritten; the temporary one is
        removed whenever the save fails.
        """
        temporary = "music-hub-temp-" + secrets.token_hex(6)
        try:
            self.run(["playlistadd " + quote(temporary) + " " + quote(uri)
                      for uri in uris])
            self.run(["rename " + quote(temporary) + " " + quote(name)])
        except MpdError:
            try:
                self.run(["rm " + quote(temporary)])
            except MpdError:
                pass  # It may never have been created.
            raise

    def control(self, action: str) -> None:
        """Send one playback control from CONTROLS."""
        if action not in CONTROLS:
            raise MpdError("unknown control")
        self.run([CONTROLS[action]])

    def status(self) -> dict:
        """Playback state and the current song's display fields."""
        pairs = self.run(["status", "currentsong"])
        info = dict(pairs)
        return {
            "state": info.get("state", "stop"),
            "title": info.get("Title", ""),
            "artist": info.get("Artist", ""),
            "album": info.get("Album", ""),
            "name": info.get("Name", ""),
            "file": info.get("file", ""),
        }
