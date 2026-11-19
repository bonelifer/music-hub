#!/usr/bin/env python3
"""Install a myMPD Home tile that opens the music-hub page.

The tile uses myMPD's built-in "hub" icon in white on myMPD's green
(#28a745), so no image file is copied into myMPD's workdir. It opens the
given URL in a new browser tab. Running the installer again updates the
existing music-hub tile in place instead of adding a second one.

Set MYMPD_SESSION when myMPD is protected by a PIN.
Exit status: 0 on success, 1 when myMPD cannot be reached or refuses the
change, 2 for invalid arguments.
"""

import argparse
import json
import os
from urllib.error import URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

TILE_NAME = "music-hub"
TILE_COMMAND = "openExternalLink"
TILE_ICON = "hub"
# myMPD's own highlight green, as used across its stylesheet.
MYMPD_GREEN = "#28a745"


def rpc(endpoint: str, method: str, params: dict) -> dict:
    """Call one myMPD JSON-RPC method and return its result object."""
    headers = {"Content-Type": "application/json"}
    if session := os.environ.get("MYMPD_SESSION"):
        headers["X-myMPD-Session"] = session
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
    }).encode()
    with urlopen(Request(endpoint, body, headers), timeout=10) as response:
        data = json.load(response)
    if "error" in data or "result" not in data:
        message = (data.get("error") or {}).get("message", "no result")
        raise ValueError(f"myMPD rejected {method}: {message}")
    return data["result"]


def tile_params(page_url: str) -> dict:
    """Build the MYMPD_API_HOME_ICON_SAVE parameters for a new tile."""
    return {
        "replace": False, "oldpos": 0, "name": TILE_NAME,
        "ligature": TILE_ICON, "bgcolor": MYMPD_GREEN,
        "color": "#ffffff", "image": "", "cmd": TILE_COMMAND,
        # The second option tells myMPD to open the link in a new tab.
        "options": [page_url, "true"],
    }


def install(endpoint: str, page_url: str) -> bool:
    """Create or update the tile; return True when an existing one was replaced."""
    tiles = rpc(endpoint, "MYMPD_API_HOME_ICON_LIST", {}).get("data") or []
    params = tile_params(page_url)
    for position, tile in enumerate(tiles):
        if tile.get("name") == TILE_NAME and tile.get("cmd") == TILE_COMMAND:
            params.update(replace=True, oldpos=position)
            break
    rpc(endpoint, "MYMPD_API_HOME_ICON_SAVE", params)
    return params["replace"]


def valid_http_url(url: str) -> bool:
    """Accept only plain HTTP(S) URLs without credentials, queries or spaces."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (parts.scheme in {"http", "https"} and bool(parts.hostname)
            and parts.username is None and not parts.query
            and not parts.fragment and url.isascii()
            and not any(ord(char) <= 32 for char in url))


def main() -> None:
    """Validate the arguments and install the Home tile."""
    parser = argparse.ArgumentParser(
        description="Add or update a myMPD Home tile that opens music-hub.")
    parser.add_argument("-m", "--mympd", required=True,
                        help="myMPD base URL, for example http://127.0.0.1:8081")
    parser.add_argument("-U", "--url", required=True,
                        help="music-hub page URL the tile opens, as the browser "
                             "that shows myMPD can reach it (use the LAN address "
                             "for phones)")
    parser.add_argument("-p", "--partition", default="default",
                        help="myMPD partition whose Home screen gets the tile "
                             "(default: default)")
    args = parser.parse_args()
    for option, value in (("--mympd", args.mympd), ("--url", args.url)):
        if not valid_http_url(value):
            parser.error(f"{option} must be an HTTP(S) URL without credentials "
                         "or queries")
    endpoint = f"{args.mympd.rstrip('/')}/api/{quote(args.partition, safe='')}"
    try:
        replaced = install(endpoint, args.url)
    except (OSError, URLError, ValueError) as error:
        parser.exit(1, f"Installation failed: {error}\n")
    action = "updated" if replaced else "added"
    print(f"music-hub Home tile {action}; it opens {args.url}")


if __name__ == "__main__":
    main()
