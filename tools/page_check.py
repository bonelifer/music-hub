#!/usr/bin/env python3
"""Accessibility and playback check of a running music-hub page.

For every enabled service view, in Light, Dark and Auto colors at desktop
and phone width, it runs axe-core and checks the page never scrolls
sideways. Then it checks the theme button by keyboard: its name, the
tooltip on focus, the focus ring, and that Enter steps the theme. With
--play, it also starts the first SomaFM channel in this browser and checks
audio actually plays.

It changes the saved theme while it runs and puts it back to Auto. It never
presses "Play on MPD".

Needs Playwright for Python with its Chromium, and a local copy of
axe-core (npm install axe-core).

Exit code: 0 when every check passed, 1 otherwise.
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

VIEWPORTS = {"desktop": (1024, 768), "phone": (375, 812)}
THEMES = ("light", "dark", "auto")


def set_theme(page, theme: str) -> None:
    """Save a theme through the page's own API, then reload."""
    page.evaluate(
        """theme => fetch('/api/theme', {method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-Music-Hub': '1'},
            body: JSON.stringify({theme})})""", theme)
    page.reload()


def wait_for_view(page) -> None:
    """Wait until the current view has finished loading."""
    page.wait_for_function(
        "!document.querySelector('#view').textContent.includes('Loading')",
        timeout=120000)


def run_axe(page, axe_source: str) -> list:
    """axe-core violations on the whole page, as readable lines."""
    page.add_script_tag(content=axe_source)
    result = page.evaluate("axe.run(document, {resultTypes: ['violations']})")
    return [f"{item['id']}: {item['help']} ({len(item['nodes'])} element(s))"
            for item in result["violations"]]


def check_views(page, base: str, axe_source: str, failures: list) -> None:
    """axe and sideways-scroll checks for every view, theme and width."""
    services = page.evaluate("fetch('/api/config').then(r => r.json())")["services"]
    for name, (width, height) in VIEWPORTS.items():
        page.set_viewport_size({"width": width, "height": height})
        for theme in THEMES:
            set_theme(page, theme)
            for service in services:
                page.goto(f"{base}/#{service['name']}")
                wait_for_view(page)
                where = f"{service['label']}, {theme}, {name}"
                for problem in run_axe(page, axe_source):
                    failures.append(f"{where}: {problem}")
                scroll = page.evaluate(
                    "document.documentElement.scrollWidth - window.innerWidth")
                if scroll > 0:
                    failures.append(f"{where}: page scrolls sideways by {scroll}px")
                print(f"checked {where}")


def check_theme_button(page, failures: list) -> None:
    """Keyboard check of the theme button."""
    set_theme(page, "auto")
    button = page.locator("#theme-button")
    name = button.evaluate("b => b.textContent.trim()")
    if not name.startswith("Theme: Auto"):
        failures.append(f"theme button name is {name!r}")
    button.focus()
    page.keyboard.press("Tab")
    page.keyboard.press("Shift+Tab")
    if page.evaluate("document.activeElement.id") != "theme-button":
        failures.append("theme button cannot be reached by keyboard")
    if not page.locator("#theme-tip").is_visible():
        failures.append("theme tooltip does not show on keyboard focus")
    outline = button.evaluate("b => getComputedStyle(b).outlineStyle")
    if outline in ("none", ""):
        failures.append("theme button has no visible focus ring")
    for expected in ("light", "dark", None):
        page.keyboard.press("Enter")
        page.wait_for_timeout(500)
        current = page.evaluate("document.documentElement.getAttribute('data-theme')")
        if current != expected:
            failures.append(f"Enter on theme button gave {current!r}, expected {expected!r}")
    print("checked theme button")


def check_playback(page, base: str, failures: list) -> None:
    """Play the first SomaFM channel in this browser and confirm it plays."""
    page.goto(f"{base}/#somafm")
    wait_for_view(page)
    button = page.locator("button[title='Play in this browser']").first
    if not button.count():
        failures.append("playback: no SomaFM 'Play here' button (is SomaFM enabled?)")
        return
    button.click()
    try:
        page.wait_for_function(
            "document.getElementById('audio').currentTime > 1", timeout=20000)
        print("checked browser playback")
    except Exception:  # noqa: BLE001 - report any wait failure the same way
        status = page.locator("#status").text_content()
        failures.append(f"playback: audio did not play ({status})")
    page.evaluate("document.getElementById('audio').pause()")


def main() -> int:
    """Run the checks and print every failure."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("url", help="music-hub page, such as http://127.0.0.1:18773")
    parser.add_argument("axe", type=Path, help="Path to axe-core's axe.min.js")
    parser.add_argument("-p", "--play", action="store_true",
                        help="Also check that a SomaFM channel plays in the browser")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    axe_source = args.axe.read_text(encoding="utf-8")
    failures = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=["--autoplay-policy=no-user-gesture-required"])
        # The page's security policy blocks injected scripts, axe included.
        # SomaFM refuses streams to a "HeadlessChrome" user agent, so the
        # check presents itself as ordinary Chrome.
        agent = browser.new_page().evaluate("navigator.userAgent")
        page = browser.new_page(bypass_csp=True,
                                user_agent=agent.replace("HeadlessChrome", "Chrome"))
        page.goto(base + "/")
        try:
            check_views(page, base, axe_source, failures)
            page.set_viewport_size({"width": 1024, "height": 768})
            check_theme_button(page, failures)
            if args.play:
                check_playback(page, base, failures)
        finally:
            set_theme(page, "auto")
            browser.close()
    for failure in failures:
        print("FAIL:", failure)
    print("All checks passed." if not failures else f"{len(failures)} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
