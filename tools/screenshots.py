#!/usr/bin/env python3
"""Take the README screenshots of a running music-hub page.

Saves PNGs into docs/images/screenshots/. It only looks at the page: it
sets the theme for each shot through the page's API and puts it back to
Auto at the end, and it never presses a play button.

Needs Playwright for Python with its Chromium.

Exit code: 0 when every screenshot was saved, 1 otherwise.
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "images" / "screenshots"
# (file name, view, theme, width, height, what to open before the shot)
SHOTS = [
    ("ibroadcast-albums.png", "ibroadcast", "light", 1100, 760, "albums"),
    ("somafm-dark.png", "somafm", "dark", 1100, 760, None),
    ("tunein-favorites.png", "tunein", "light", 1100, 760, None),
    ("iheart-phone-dark.png", "iheart", "dark", 390, 844, None),
]


def set_theme(page, theme: str) -> None:
    """Save a theme through the page's own API."""
    page.evaluate(
        """theme => fetch('/api/theme', {method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-Music-Hub': '1'},
            body: JSON.stringify({theme})})""", theme)


def main() -> int:
    """Take every screenshot in SHOTS."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("url", help="music-hub page, such as http://127.0.0.1:18773")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        # The page's security policy blocks Playwright's waiting helpers.
        page = browser.new_page(bypass_csp=True)
        page.goto(base + "/")
        try:
            for name, view, theme, width, height, tab in SHOTS:
                page.set_viewport_size({"width": width, "height": height})
                set_theme(page, theme)
                page.goto(f"{base}/#{view}")
                page.reload()
                page.wait_for_function(
                    "!document.querySelector('#view').textContent.includes('Loading')",
                    timeout=120000)
                if tab == "albums":
                    page.get_by_role("button", name="Albums", exact=True).click()
                    page.wait_for_selector(".rows li", timeout=120000)
                    page.get_by_label("Filter albums").fill("Abbey Road")
                # Let lazy station logos finish loading.
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(800)
                page.screenshot(path=str(OUTPUT / name))
                print(f"saved {OUTPUT / name}")
        finally:
            set_theme(page, "auto")
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
