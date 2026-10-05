#!/usr/bin/env python3
"""Render the user guide to a real PDF.

    python3 scripts/guide-pdf.py <source.html> <out.pdf>

Why this exists rather than window.print(): the browser's print dialog is not a
download. It hands the reader a preview and makes them choose "Save as PDF"
themselves, and Chrome stamps its own header and footer onto every page, so a
guide meant for a dealership arrives with "9/7/26, 11:47 AM" and a ts.net URL
printed across it. Neither can be controlled from the page.

Chromium's own PDF writer, driven directly, produces a proper text PDF: the
words are selectable and searchable, not a picture of the page. The header and
footer are simply off.

Run by the collector's interpreter, which is the only one here with a browser
installed. Same reason as backup.py and check.py.
"""
from __future__ import annotations

import pathlib
import sys

from playwright.sync_api import sync_playwright


def render(source: pathlib.Path, out: pathlib.Path) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        # Loaded from disk rather than over HTTP, so this never needs a session
        # and can never be a way around the login on /guide.
        page.goto(source.resolve().as_uri(), wait_until="networkidle")
        # The webfonts come from Google and the page is unreadable in a fallback
        # face, so wait for them rather than racing the render.
        page.wait_for_timeout(1200)
        # The questions are collapsed on screen. On paper every answer has to
        # show, and print emulation does not fire the page's beforeprint hook.
        page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
        page.emulate_media(media="print")
        out.parent.mkdir(parents=True, exist_ok=True)
        page.pdf(
            path=str(out),
            format="A4",
            print_background=True,
            display_header_footer=False,
            margin={"top": "16mm", "bottom": "16mm", "left": "14mm", "right": "14mm"},
        )
        browser.close()


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    source, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    if not source.exists():
        print(f"No such file: {source}", file=sys.stderr)
        return 1
    render(source, out)
    print(f"{out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
