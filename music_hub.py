#!/usr/bin/env python3
"""music-hub: a web page for the mympd-* music services, outside myMPD.

Browse iBroadcast and play TuneIn, iHeartRadio and SomaFM stations from
any browser. Music plays on MPD, in the browser, or either, as
music-hub.cfg allows. myMPD stays the main way in; this is a second one,
with its own settings.

Usage:
  music_hub.py serve [--config PATH] [--state-dir PATH]
  music_hub.py check-config [--config PATH]

Run with no arguments to see this help.
"""

import argparse
import os
import sys
from pathlib import Path

import config as config_module

HERE = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    """The command-line interface."""
    parser = argparse.ArgumentParser(
        prog="music_hub.py",
        description="A web page for the mympd-* music services, outside myMPD.")
    config_default = os.environ.get("MUSIC_HUB_CONFIG", str(HERE / "music-hub.cfg"))
    state_default = os.environ.get("MUSIC_HUB_STATE", str(HERE / "state"))
    sub = parser.add_subparsers(dest="operation", metavar="OPERATION")

    serve = sub.add_parser("serve", help="Run the web server until stopped")
    serve.add_argument("-c", "--config", type=Path, default=Path(config_default),
                       help="Settings file to read (default: %(default)s, or "
                            "$MUSIC_HUB_CONFIG)")
    serve.add_argument("-s", "--state-dir", type=Path, default=Path(state_default),
                       help="Folder for saved choices such as the theme "
                            "(default: %(default)s, or $MUSIC_HUB_STATE)")

    check = sub.add_parser("check-config",
                           help="Read the settings file, report problems, and exit")
    check.add_argument("-c", "--config", type=Path, default=Path(config_default),
                       help="Settings file to check (default: %(default)s, or "
                            "$MUSIC_HUB_CONFIG)")
    return parser


def main(argv=None) -> int:
    """Run the chosen operation; exit 0 on success, 1 on a settings error."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.operation is None:
        parser.print_help()
        return 0
    try:
        if args.operation == "check-config":
            settings = config_module.load(args.config)
            enabled = ", ".join(item.label for item in settings.enabled_services())
            print(f"{args.config} is valid. Services: {enabled or 'none'}.")
            return 0
        import web  # Imported here so --help works without the server code.
        web.serve(args.config, args.state_dir)
        return 0
    except config_module.ConfigError as error:
        print(f"music-hub: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"music-hub: could not start: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
