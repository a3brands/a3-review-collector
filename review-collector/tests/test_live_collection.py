"""LIVE tests against the two real Google Business Profiles.

These are the only tests that touch Google. They are skipped by default because
they depend on the network, on Google's current behaviour, and on whichever
backend you have configured.

    RUN_LIVE_TESTS=1 pytest tests/test_live_collection.py -v -s

A failure here is informative, not necessarily a bug: it usually means Google is
serving this machine the signed-out "limited view" with no reviews. Run
`python scripts/diagnose_maps.py` to confirm.
"""
from __future__ import annotations

import os
import time

import pytest

from app.collector.base import AccessBlocked, CollectorError
from app.collector.registry import get_backends
from app.config import get_settings

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_TESTS") != "1",
        reason="Live Google tests are opt-in: set RUN_LIVE_TESTS=1",
    ),
]


class _Business:
    """A stand-in for the DB row, built straight from .env."""

    def __init__(self, cfg):
        self.key = cfg.key
        self.name = cfg.name
        self.place_id = cfg.place_id
        self.google_url = cfg.google_url
        self.gbp_location_name = cfg.gbp_location_name


def _configured():
    return [_Business(c) for c in get_settings().businesses() if c.is_configured()]


@pytest.mark.parametrize("key", ["bmw_fwb", "mb_fwb"])
def test_live_collect_real_reviews(key):
    """Live Test 1 / 2: collect real reviews for each dealership.

    Google serves the reviews-bearing layout only some of the time, so this
    mirrors production and gives the backend several independent sessions
    before declaring failure.
    """
    settings = get_settings()
    business = next((b for b in _configured() if b.key == key), None)
    if business is None:
        pytest.skip(f"{key} is not configured in .env")

    backends = get_backends(settings)
    assert backends, "no collector backend configured"

    attempts = int(os.getenv("LIVE_ATTEMPTS", "4"))
    last_error = None

    for backend in backends:
        available, reason = backend.is_available()
        if not available:
            print(f"\n  backend {backend.name}: unavailable -- {reason}")
            continue

        for attempt in range(1, attempts + 1):
            try:
                result = backend.collect(business, 10)
            except AccessBlocked as exc:
                last_error = exc
                print(f"\n  {backend.name} attempt {attempt}/{attempts}: reduced listing served")
                time.sleep(3)
                continue
            except CollectorError as exc:
                last_error = exc
                print(f"\n  {backend.name} attempt {attempt}/{attempts}: {exc}")
                time.sleep(3)
                continue

            print(f"\n  {business.name} via {result.backend}: {len(result.reviews)} review(s) "
                  f"on attempt {attempt}")
            for note in result.notes:
                print(f"    note: {note}")
            for review in result.reviews[:5]:
                print(f"    {review.rating}* {review.reviewer_name!r} "
                      f"{(review.review_text or '')[:60]!r} date={review.review_date} "
                      f"approx={review.review_date_is_approximate} id={review.source_review_id!r}")

            assert result.reviews, "backend returned zero reviews"
            for review in result.reviews:
                assert review.has_content()
                if review.rating is not None:
                    assert 1 <= review.rating <= 5
            return

    pytest.fail(
        f"No backend collected reviews for {business.name} in {attempts} attempts. "
        f"Last error: {last_error}\n"
        "This is a real, reported failure -- not silently ignored. "
        "Run scripts/diagnose_maps.py, or configure COLLECTOR_BACKEND=gbp_api."
    )


def test_live_place_ids_point_at_fort_walton_beach():
    """Guard against ever monitoring Fort Washington instead of Fort Walton Beach.

    Google Maps is JavaScript-rendered, so this has to use a real browser -- the
    raw HTML response is only an empty app shell.
    """
    from playwright.sync_api import sync_playwright

    businesses = [b for b in _configured() if b.place_id]
    if not businesses:
        pytest.skip("no place IDs configured")

    settings = get_settings()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=settings.playwright_headless)
        context = browser.new_context(
            locale=settings.playwright_locale,
            timezone_id=settings.playwright_timezone,
            viewport={"width": 1400, "height": 950},
        )
        try:
            page = context.new_page()
            for business in businesses:
                url = (
                    "https://www.google.com/maps/place/?q=place_id:"
                    f"{business.place_id}&hl=en&gl=us"
                )
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)

                heading = page.locator("h1").first.inner_text(timeout=15000).strip()
                body = page.content()

                print(f"\n  {business.name}")
                print(f"    place_id : {business.place_id}")
                print(f"    heading  : {heading}")

                assert business.name.lower().replace("-", " ") in heading.lower().replace("-", " "), (
                    f"{business.name}: the listing that loaded is titled {heading!r}"
                )
                assert "Fort Walton Beach" in body, f"{business.name}: not Fort Walton Beach"
                assert "Fort Washington" not in body, f"{business.name}: resolved to Fort WASHINGTON"
                print("    verified : Fort Walton Beach, FL")
        finally:
            context.close()
            browser.close()
