#!/usr/bin/env python3
"""Report whether the public Google Maps backend can actually work here.

Google serves some networks a signed-out "limited view" of Maps that contains no
reviews at all. This tells you which view YOU get, before you rely on it.

    python scripts/diagnose_maps.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collector.google_maps import LIMITED_VIEW_MARKERS  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.services.business_registry import active_business_configs  # noqa: E402


def main() -> None:
    from playwright.sync_api import sync_playwright

    settings = get_settings()
    # Every active dealership, not just the two seeded from .env. Reading
    # settings.businesses() here meant this reported on 2 of 6 and looked clean.
    businesses = [b for b in active_business_configs() if b.is_configured()]
    if not businesses:
        raise SystemExit(
            "No active dealerships with a place_id. Add one at /admin, or fill in "
            "PLACE_IDs in .env for the seeded pair."
        )
    print(f"{len(businesses)} active dealership(s) to check.")

    print("Checking what Google serves this machine...\n")
    verdicts = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=settings.playwright_headless)
        context = browser.new_context(
            locale=settings.playwright_locale,
            timezone_id=settings.playwright_timezone,
            viewport={"width": 1400, "height": 950},
        )
        context.add_cookies([{
            "name": "SOCS",
            "value": "CAISNQgQEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjQwMzE5LjAxX3AxGgJlbiADGgYIgLC_rwY",
            "domain": ".google.com", "path": "/",
        }])
        page = context.new_page()

        for business in businesses:
            url = f"https://www.google.com/maps/place/?q=place_id:{business.place_id}&hl=en&gl=us"
            print(f"--- {business.name} ---")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)
            except Exception as exc:
                print(f"  LOAD FAILED: {exc}\n")
                verdicts.append(False)
                continue

            if "/sorry/" in page.url:
                print("  BLOCKED: Google served a CAPTCHA interstitial.\n")
                verdicts.append(False)
                continue

            content = page.content()
            heading = ""
            try:
                heading = page.locator("h1").first.inner_text(timeout=8000).strip()
            except Exception:
                pass

            limited = any(marker in content for marker in LIMITED_VIEW_MARKERS)
            cards = page.locator("div[data-review-id]").count()
            tabs = [
                page.locator("button[role='tab']").nth(i).get_attribute("aria-label")
                for i in range(page.locator("button[role='tab']").count())
            ]

            print(f"  listing loaded : {heading or '(no heading)'}")
            print(f"  identity match : {business.name.lower() in heading.lower()}")
            print(f"  limited view   : {limited}")
            print(f"  tabs           : {tabs}")
            print(f"  review cards   : {cards}")
            usable = cards > 0 or not limited
            print(f"  VERDICT        : {'reviews reachable' if cards > 0 else 'NO reviews reachable'}\n")
            verdicts.append(cards > 0)

        context.close()
        browser.close()

    if all(verdicts) and verdicts:
        print("RESULT: the playwright backend can collect reviews on this network.")
    else:
        print(
            "RESULT: the playwright backend CANNOT collect reviews on this network.\n"
            "Google is serving the signed-out limited view (no review list).\n"
            "Use COLLECTOR_BACKEND=gbp_api instead -- it is free and supported.\n"
            "See README section 'Google access reality check'."
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
