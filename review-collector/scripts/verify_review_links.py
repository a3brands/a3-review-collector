#!/usr/bin/env python3
"""Open real review links on Google and check each lands in the right place.

For every active dealership, takes its newest reviews that have a reviewer
name, opens each stored link in headless Chromium, and checks the page shows:

  - the dealership's name above the review (the listing panel), and
  - that reviewer's name (the exact comment, not the general review list).

A link missing the listing id (`0x0:0x0`) fails before it is even opened.
Screenshots go to data/link-check/ so a failure can be looked at.

    python scripts/verify_review_links.py              # 1 review per dealership
    python scripts/verify_review_links.py --per 3      # 3 per dealership
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from app.database.database import init_db, session_scope  # noqa: E402
from app.database.models import Business, Review  # noqa: E402

SHOTS = Path(__file__).resolve().parent.parent / "data" / "link-check"
CONSENT = {
    "name": "SOCS",
    "value": "CAISNQgQEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjQwMzE5LjAxX3AxGgJlbiADGgYIgLC_rwY",
    "domain": ".google.com",
    "path": "/",
}


def google_name(business) -> str:
    """The name Google shows, which is not always the one configured.

    "Findlay Subaru of Las Vegas" is listed as "Subaru of Las Vegas", so the
    name is read from the listing URL when it carries one.
    """
    for url in (business.google_url, business.configured_url):
        match = re.search(r"/maps/place/([^/@?]+)", url or "")
        if match:
            return urllib.parse.unquote_plus(match.group(1))
    return business.name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--per", type=int, default=1, help="reviews to check per dealership")
    args = parser.parse_args()

    init_db()
    cases = []
    with session_scope() as session:
        for business in session.query(Business).filter_by(active=True).order_by(Business.id):
            rows = (
                session.query(Review)
                .filter(Review.business_id == business.id,
                        Review.reviewer_name.isnot(None),
                        Review.review_url.isnot(None))
                .order_by(Review.detected_at.desc())
                .limit(args.per)
                .all()
            )
            for row in rows:
                cases.append((business.key, business.name, google_name(business),
                              row.reviewer_name, row.review_url))

    SHOTS.mkdir(parents=True, exist_ok=True)
    failures = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(locale="en-US", viewport={"width": 1400, "height": 950})
        context.add_cookies([CONSENT])
        for key, name, listed_as, reviewer, url in cases:
            problems = []
            if "0x0:0x0" in url:
                problems.append("link has no listing id (0x0:0x0)")
            else:
                page = context.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_function(
                        "r => document.body.innerText.includes(r)", arg=reviewer, timeout=20000)
                except Exception:
                    pass
                text = page.inner_text("body")
                if listed_as.lower() not in text.lower():
                    problems.append(f"dealership name '{listed_as}' not on the page")
                if reviewer not in text:
                    problems.append(f"reviewer '{reviewer}' not on the page")
                page.screenshot(path=str(SHOTS / f"{key}.png"))
                page.close()

            status = "PASS" if not problems else "FAIL"
            failures += bool(problems)
            print(f"{status}  {name:<36} {reviewer}")
            for problem in problems:
                print(f"      - {problem}")
        browser.close()

    print(f"\n{len(cases) - failures}/{len(cases)} passed. Screenshots: {SHOTS}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
