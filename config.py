"""Read and check music-hub.cfg.

The file holds this web UI's own preferences. They are separate from the
myMPD screens' settings: changing one never changes the other. Logins are
not kept here; they belong to the bridges, which this UI only calls.
"""

import configparser
import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

# Services this version can show, in menu order, with their display names.
SERVICES = {
    "ibroadcast": "iBroadcast",
    "tunein": "TuneIn",
    "iheart": "iHeartRadio",
    "somafm": "SomaFM",
}
# Where a random iBroadcast playlist can be saved.
DESTINATIONS = ("mpd", "ibroadcast", "both")


class ConfigError(ValueError):
    """A setting is missing or has a value of the wrong kind."""


@dataclass
class Service:
    """One music service and the bridge that serves it."""

    name: str
    label: str
    enabled: bool
    bridge_url: str


@dataclass
class Config:
    """Every setting music-hub uses, already checked."""

    listen: str = "127.0.0.1"
    port: int = 18773
    allowed_hosts: tuple = ()
    mpd_enabled: bool = True
    browser_enabled: bool = True
    mpd_host: str = "127.0.0.1"
    mpd_port: int = 6600
    mpd_password: str = ""
    random_playlist_size: int = 25
    playlist_destination: str = "mpd"
    services: dict = field(default_factory=dict)

    def enabled_services(self) -> list:
        """Services that are switched on, in menu order."""
        return [service for service in self.services.values() if service.enabled]


def _bool(section, key: str, default: bool) -> bool:
    """Read a yes/no setting, naming it when the value is not one."""
    try:
        return section.getboolean(key, fallback=default)
    except ValueError:
        raise ConfigError(
            f"[{section.name}] {key} must be true or false") from None


def _int(section, key: str, default: int, low: int, high: int) -> int:
    """Read a whole number between low and high."""
    try:
        value = section.getint(key, fallback=default)
    except ValueError:
        value = None
    if value is None or not low <= value <= high:
        raise ConfigError(
            f"[{section.name}] {key} must be a whole number from {low} to {high}")
    return value


def _bridge_url(section, default: str) -> str:
    """Read a bridge address: http:// with a host and port, nothing else."""
    value = section.get("bridge_url", fallback=default).strip().rstrip("/")
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        port = None
    if (parts.scheme != "http" or not parts.hostname or port is None
            or parts.path or parts.query or parts.fragment
            or parts.username or parts.password):
        raise ConfigError(
            f"[{section.name}] bridge_url must look like http://127.0.0.1:18769")
    return value


# Default bridge ports: the ones the mympd-* bridges listen on.
DEFAULT_BRIDGES = {
    "ibroadcast": "http://127.0.0.1:18769",
    "tunein": "http://127.0.0.1:18766",
    "iheart": "http://127.0.0.1:18767",
    "somafm": "http://127.0.0.1:18768",
}


def load(path: Path) -> Config:
    """Read the config file; raise ConfigError naming any bad setting.

    A missing file is an error, so a typo in the path is not mistaken for
    an empty config. Settings left out of the file use the defaults.
    """
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8") as handle:
            parser.read_file(handle)
    except FileNotFoundError:
        raise ConfigError(
            f"{path} does not exist; copy music-hub.cfg.example to it") from None
    except (OSError, UnicodeError, configparser.Error) as error:
        raise ConfigError(f"Could not read {path}: {error}") from None
    for name in ("server", "playback", "mpd", *SERVICES):
        if not parser.has_section(name):
            parser.add_section(name)

    config = Config()
    server = parser["server"]
    config.listen = server.get("listen", fallback=config.listen).strip()
    try:
        ipaddress.ip_address(config.listen)
    except ValueError:
        raise ConfigError(
            "[server] listen must be an IP address such as 127.0.0.1") from None
    config.port = _int(server, "port", config.port, 1, 65535)
    hosts = server.get("allowed_hosts", fallback="")
    config.allowed_hosts = tuple(dict.fromkeys(
        [host.strip().lower() for host in hosts.split(",") if host.strip()]
        + ["127.0.0.1", "localhost", "[::1]", config.listen]))

    playback = parser["playback"]
    config.mpd_enabled = _bool(playback, "mpd_enabled", True)
    config.browser_enabled = _bool(playback, "browser_enabled", True)
    if not (config.mpd_enabled or config.browser_enabled):
        raise ConfigError(
            "[playback] turn on mpd_enabled, browser_enabled, or both")

    mpd = parser["mpd"]
    config.mpd_host = mpd.get("host", fallback=config.mpd_host).strip()
    if not config.mpd_host:
        raise ConfigError("[mpd] host must not be empty")
    config.mpd_port = _int(mpd, "port", config.mpd_port, 1, 65535)
    config.mpd_password = mpd.get("password", fallback="")
    if any(character in config.mpd_password for character in "\r\n\0"):
        raise ConfigError("[mpd] password must be on one line")

    for name, label in SERVICES.items():
        section = parser[name]
        config.services[name] = Service(
            name=name, label=label,
            enabled=_bool(section, "enabled", True),
            bridge_url=_bridge_url(section, DEFAULT_BRIDGES[name]))

    ibroadcast = parser["ibroadcast"]
    config.random_playlist_size = _int(
        ibroadcast, "random_playlist_size", config.random_playlist_size, 1, 100)
    destination = ibroadcast.get(
        "playlist_destination", fallback=config.playlist_destination).strip()
    if destination not in DESTINATIONS:
        raise ConfigError(
            "[ibroadcast] playlist_destination must be mpd, ibroadcast, or both")
    if destination != "ibroadcast" and not config.mpd_enabled:
        # Without MPD the only place a playlist can go is iBroadcast.
        destination = "ibroadcast"
    config.playlist_destination = destination
    return config
