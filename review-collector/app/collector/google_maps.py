"""Public Google Maps backend -- credential-free.

MEASURED BEHAVIOUR (2026-08-19, against the two configured dealerships):

  Google decides per session whether to serve the full listing (a "Reviews for
  <business>" tab, a sort control and a lazy-loading review list) or a reduced
  one carrying only Overview/About and no reviews at all.

  Two things decide it, and both are needed:

    1. The User-Agent. Playwright's headless build advertises "HeadlessChrome",
       and Google serves that string the reduced listing almost every time.
    2. How you arrive. Deep-linking cold to a place URL is worse than browsing
       google.com -> maps.google.com first, which lets Google set its ordinary
       session cookies before the place request.

       HeadlessChrome UA + warm-up ....  1/6 full listings
       desktop Chrome UA, cold ........  2/8 full listings
       desktop Chrome UA + warm-up .... 14/14 full listings

  So this backend sets a standard desktop Chrome UA (PLAYWRIGHT_USER_AGENT) and
  warms the session (PLAYWRIGHT_WARM_SESSION) before every collection.

  On the UA specifically -- this is a judgement call and it is deliberately
  configurable rather than hidden. The browser really is Chromium, the reviews
  are public, and no credential, CAPTCHA or access control is being defeated;
  setting a UA is a documented, routine Playwright feature. But it does mean
  presenting a different client string than the default in order to be served
  the normal page. If you would rather not, set PLAYWRIGHT_USER_AGENT="" and use
  COLLECTOR_BACKEND=gbp_api, which needs none of this.

  What this backend still never does: sign in, solve a CAPTCHA, rotate proxies,
  patch navigator.webdriver, or otherwise defeat an access control. If Google
  serves the reduced listing anyway it raises ``AccessBlocked`` and the check is
  recorded as FAILED. No review is ever invented.

  Two caveats on the data itself:
    * Public review dates are relative ("2 weeks ago"), so review_date is
      approximate and flagged via review_date_is_approximate.
    * The list is explicitly sorted newest-first, because Google's default
      "Most relevant" order can hide a brand-new review below older ones.
"""
from __future__ import annotations

import concurrent.futures
import logging
import re
import urllib.parse
from typing import List, Optional

from app.collector.base import (
    AccessBlocked,
    BackendUnavailable,
    CollectionResult,
    CollectorBackend,
    ParseError,
    RawReview,
    TransientCollectorError,
)
from app.collector.parser import parse_maps_card
from app.config import Settings

logger = logging.getLogger(__name__)

# Landing on Google and then Maps before opening the place page lets Google
# establish its ordinary session cookies first. Cold-deep-linking straight to a
# place URL is what triggers the reviews-free "reduced listing" -- measured at
# 2/8 full listings cold versus 8/8 warmed, across both dealerships.
# This is plain navigation, exactly what a person does. No sign-in, no CAPTCHA
# handling, no fingerprint spoofing.
WARMUP_URLS = (
    "https://www.google.com/?hl=en&gl=us",
    "https://www.google.com/maps?hl=en&gl=us",
)

LIMITED_VIEW_MARKERS = (
    "limited view of Google Maps",
    "You're seeing a limited view",
)

# Extracts every review card currently in the DOM. Kept as one in-page script so
# that all the brittle, Google-specific selectors live in exactly one place.
# The listing's own headline figures. review-store.js is explicit that these must
# come from the platform rather than being recomputed from the reviews on hand --
# a partial pull skews high, because recent reviews are more positive.
LISTING_STATS_SCRIPT = r"""
() => {
  const text = document.body.innerText || '';
  // "4.5" next to a "N reviews" count, as the listing header renders it.
  const rating = text.match(/(\d[.,]\d)\s*(?:\n|\s)*\(?\s*([\d,]+)\s*reviews?\)?/i);
  if (rating) {
    return {
      totalScore: parseFloat(rating[1].replace(',', '.')),
      reviewsCount: parseInt(rating[2].replace(/,/g, ''), 10)
    };
  }
  const ratingOnly = text.match(/^\s*(\d[.,]\d)\s*$/m);
  return {
    totalScore: ratingOnly ? parseFloat(ratingOnly[1].replace(',', '.')) : null,
    reviewsCount: null
  };
}
"""

EXTRACT_SCRIPT = r"""
() => {
  const cards = Array.from(document.querySelectorAll('div[data-review-id]'))
      .filter(el => el.querySelector('span[role="img"][aria-label*="star" i], span[aria-label*="star" i]'));
  const seen = new Set();
  const out = [];
  for (const el of cards) {
    const id = el.getAttribute('data-review-id');
    if (!id || seen.has(id)) continue;
    seen.add(id);

    const starEl = el.querySelector('span[role="img"][aria-label*="star" i], span[aria-label*="star" i]');
    const authorLink = el.querySelector('button[data-href*="/maps/contrib/"], a[href*="/maps/contrib/"]');
    const nameEl = el.querySelector('div.d4r55, .d4r55, button[data-href*="/maps/contrib/"] div');
    // The customer's own comment lives in div.MyEned. The owner's reply to it
    // lives in a separate block that ALSO contains a span.wiI7pd. Falling back
    // to a bare '.wiI7pd' therefore stores the dealership's own reply as if the
    // customer had written it, whenever the customer left only a star rating.
    // So: read div.MyEned or nothing at all. A star-only review must come back
    // as null, never as somebody else's text.
    let ownText = null;
    const ownEl = el.querySelector('div.MyEned');
    if (ownEl) {
      const clone = ownEl.cloneNode(true);
      clone.querySelectorAll('div.CDe7pd, div[class*="CDe"], div[class*="owner"]')
           .forEach(n => n.remove());
      ownText = clone.textContent || '';
      const marker = ownText.search(/Response from the owner/i);
      if (marker > -1) ownText = ownText.slice(0, marker);
      ownText = ownText.trim() || null;
    }
    const dateEl = el.querySelector('span.rsqaWe, .rsqaWe, span.xRkPPb');

    // Whether the dealership already answered. Google renders the header
    // ("Response from the owner - 2 months ago") on the reviews tab but often
    // not the reply body, so presence is what we can rely on; the text is
    // captured when it happens to be there.
    let ownerReplied = false, ownerReplyText = null, ownerReplyDate = null;
    const marker = /Response from the owner/i;
    if (marker.test(el.textContent || '')) {
      ownerReplied = true;
      const blocks = Array.from(el.querySelectorAll('div'))
        .filter(d => marker.test((d.textContent || '').trim().slice(0, 40)))
        .sort((a, b) => (a.textContent || '').length - (b.textContent || '').length);
      const block = blocks[0];
      if (block) {
        const whole = (block.textContent || '').trim();
        const rest = whole.replace(marker, '').trim();
        const age = rest.match(/^([^\n]*?\bago)\b/i);
        ownerReplyDate = age ? age[1].trim() : null;
        const body = age ? rest.slice(age[0].length).trim() : rest;
        ownerReplyText = body.length ? body : null;
      }
    }

    out.push({
      review_id: id,
      owner_replied: ownerReplied,
      owner_reply: ownerReplyText,
      owner_reply_date_text: ownerReplyDate,
      rating_label: starEl ? starEl.getAttribute('aria-label') : null,
      reviewer_name: nameEl ? nameEl.textContent : null,
      reviewer_profile_url: authorLink
        ? (authorLink.getAttribute('href') || authorLink.getAttribute('data-href'))
        : null,
      text: ownText,
      date_text: dateEl ? dateEl.textContent : null,
      review_url: null
    });
  }
  return out;
}
"""


class GoogleMapsBackend(CollectorBackend):
    name = "google_maps"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._availability: Optional[tuple] = None

    # ------------------------------------------------------------------
    def is_available(self) -> tuple[bool, str]:
        """Cached probe for Playwright + Chromium.

        The probe runs on a worker thread: Playwright's sync API refuses to run
        inside a running asyncio loop, and this is called both from FastAPI's
        async startup and from ordinary threadpool request handlers.
        """
        if self._availability is None:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                self._availability = pool.submit(self._probe).result()
        return self._availability

    @staticmethod
    def _probe() -> tuple[bool, str]:
        import os

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return False, "The 'playwright' package is not installed (pip install -r requirements.txt)."

        try:
            with sync_playwright() as pw:
                path = pw.chromium.executable_path
        except Exception as exc:  # pragma: no cover - environment dependent
            return False, f"Playwright could not start: {exc}"

        if not path or not os.path.exists(path):
            return False, "Chromium is not installed. Run: python -m playwright install chromium"
        return True, "Playwright + Chromium available."

    # ------------------------------------------------------------------
    @staticmethod
    def looks_like_a_listing(url: Optional[str]) -> bool:
        """Is this an actual listing, or something that merely mentions one?

        A Google *search* URL is the usual mistake: it is what you get from the
        address bar after typing a dealership's name, it looks plausible, and it
        loads a results page rather than a business. The collector refuses those
        rather than scraping whatever happens to be on screen, which is correct
        and also unhelpful if nobody notices.
        """
        if not url:
            return False
        return "/maps/place/" in url or "place_id:" in url

    @staticmethod
    def looks_like_a_place_id(value: Optional[str]) -> bool:
        # Google's own ids: ChIJ... or the hex feature id form 0x...:0x...
        if not value:
            return False
        return value.startswith("ChIJ") or (value.startswith("0x") and ":" in value)

    def resolve_listing(self, page, query: str) -> Optional[dict]:
        """Turn a search URL, or a plain dealership name, into the real listing.

        Returns None when Google shows a list of candidates rather than landing
        on one business. Picking from a list would mean guessing which of several
        dealerships the customer meant, and pointing their dashboard and their
        approval emails at the wrong business is far worse than refusing.
        """
        term = query.strip()
        # Pull the human part out of a search URL so Maps gets a clean query.
        match = re.search(r"[?&]q=([^&]+)", term)
        if match:
            term = urllib.parse.unquote_plus(match.group(1))
        term = re.sub(r"https?://\S+", "", term).strip()
        if not term:
            return None

        page.goto(
            "https://www.google.com/maps/search/" + urllib.parse.quote_plus(term),
            wait_until="domcontentloaded",
            timeout=self.settings.collector_timeout_seconds * 1000,
        )
        page.wait_for_timeout(6000)

        if "/maps/place/" not in page.url:
            return None

        name = (page.title() or "").replace(" - Google Maps", "").strip()
        return {"url": page.url, "name": name or term}

    @staticmethod
    def place_url(business) -> str:
        """Prefer the Place ID -- it pins the exact listing and cannot drift."""
        if business.place_id:
            return f"https://www.google.com/maps/place/?q=place_id:{business.place_id}&hl=en&gl=us"
        if business.google_url:
            url = business.google_url
            joiner = "&" if "?" in url else "?"
            return f"{url}{joiner}hl=en&gl=us"
        raise BackendUnavailable(
            f"{business.name}: neither PLACE_ID nor GOOGLE_URL is configured."
        )

    # ------------------------------------------------------------------
    def collect(self, business, limit: int,
                known_review_count: Optional[int] = None) -> CollectionResult:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright

        url = self.place_url(business)
        timeout_ms = self.settings.collector_timeout_seconds * 1000
        notes: List[str] = []
        resolved_url: Optional[str] = None
        resolved_name: Optional[str] = None

        with sync_playwright() as pw:
            browser = None
            context = None
            try:
                browser = pw.chromium.launch(headless=self.settings.playwright_headless)
                context_options = {
                    "locale": self.settings.playwright_locale,
                    "timezone_id": self.settings.playwright_timezone,
                    "viewport": {"width": 1400, "height": 950},
                }
                if self.settings.playwright_user_agent:
                    context_options["user_agent"] = self.settings.playwright_user_agent
                context = browser.new_context(**context_options)
                # Standard cookie-consent acceptance -- the same state a person
                # gets by clicking "Accept all". Not an access-control bypass.
                context.add_cookies(
                    [
                        {
                            "name": "SOCS",
                            "value": "CAISNQgQEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjQwMzE5LjAxX3AxGgJlbiADGgYIgLC_rwY",
                            "domain": ".google.com",
                            "path": "/",
                        }
                    ]
                )
                page = context.new_page()

                # The warm-up visit exists to make Google serve a full listing
                # rather than a reduced one. A check that only needs the headline
                # count does not need it, and it is a whole page load.
                fast_attempt = (
                    self.settings.fast_path_enabled and known_review_count is not None
                )
                if self.settings.playwright_warm_session and not (
                    fast_attempt and self.settings.fast_path_skip_warmup
                ):
                    self._warm_session(page, notes)

                # Configured with a search URL or a bare name rather than a
                # listing. Work out which business is meant, once, and hand it
                # back so nobody has to do it by hand.
                if not self.looks_like_a_listing(url) and not business.place_id:
                    hint = business.google_url or business.name
                    found = self.resolve_listing(page, hint)
                    if found:
                        resolved_url, resolved_name = found["url"], found["name"]
                        url = f"{resolved_url}{'&' if '?' in resolved_url else '?'}hl=en&gl=us"
                        notes.append(
                            f"Resolved to the Google listing for '{resolved_name}'."
                        )
                    else:
                        raise AccessBlocked(
                            f"{business.name}: what is configured is not a Google listing, and "
                            "searching for it returned several possible businesses rather than "
                            "one. Open the dealership on Google Maps and paste the URL from the "
                            "address bar."
                        )

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except PlaywrightTimeout as exc:
                    raise TransientCollectorError(
                        f"{business.name}: timed out loading the Google Maps listing after "
                        f"{self.settings.collector_timeout_seconds}s."
                    ) from exc

                # Was a flat four second wait on every check. Polling for the
                # headline figures instead means a page that settles in one
                # second costs one second, which matters when the whole point of
                # a fast check is that it is short.
                self._await_listing_stats(page)

                if "/sorry/" in page.url or "consent.google.com" in page.url:
                    raise AccessBlocked(
                        f"{business.name}: Google served a CAPTCHA / consent interstitial "
                        f"({page.url.split('?')[0]}). Not attempting to bypass it. "
                        "Use the gbp_api backend for reliable access."
                    )

                # The identity check exists to catch a CONFIGURED url that has
                # drifted to a different business. When the listing was resolved
                # a moment ago by searching for this dealership, there is nothing
                # to have drifted from, and checking the page title against a
                # placeholder name like "toyota-test" would reject the very
                # listing we just correctly found.
                if resolved_url:
                    notes.append(
                        f"Identity taken from the resolved listing '{resolved_name}' "
                        "rather than the configured name."
                    )
                else:
                    self._verify_listing(page, business, notes)
                listing_stats = self._read_listing_stats(page, notes)

                # The cheap check. Google publishes the listing's total review
                # count at the top of the page, before the review pane is even
                # opened. When it matches what we already hold, there is nothing
                # new and the scroll is pure cost: opening the pane and scrolling
                # it is where nearly all of a 30 second check goes.
                live_count = listing_stats.get("reviewsCount")
                if (
                    self.settings.fast_path_enabled
                    and known_review_count is not None
                    and live_count is not None
                    and int(live_count) == int(known_review_count)
                ):
                    notes.append(
                        f"Listing still reports {live_count} reviews, unchanged since the "
                        "last check, so the review pane was not opened."
                    )
                    return CollectionResult(
                        reviews=[],
                        backend=self.name,
                        total_review_count=live_count,
                        average_rating=listing_stats.get("totalScore"),
                        notes=notes,
                        skipped_unchanged=True,
                    )

                self._open_reviews(page, business, notes)
                cards = self._scroll_and_extract(page, limit)

            except PlaywrightError as exc:
                if isinstance(exc, (AccessBlocked, BackendUnavailable)):
                    raise
                raise TransientCollectorError(
                    f"{business.name}: browser error during collection: {exc}"
                ) from exc
            finally:
                for closable in (context, browser):
                    try:
                        if closable is not None:
                            closable.close()
                    except Exception:  # pragma: no cover
                        pass

        reviews = []
        for card in cards:
            raw = RawReview(**parse_maps_card(card, place_id=business.place_id))
            if raw.has_content():
                reviews.append(raw)

        if cards and not reviews:
            raise ParseError(
                f"{business.name}: found {len(cards)} review card(s) but could not extract any "
                "usable fields. Google most likely changed its markup -- update the selectors in "
                "app/collector/google_maps.py (EXTRACT_SCRIPT)."
            )

        return CollectionResult(
            reviews=reviews[:limit],
            backend=self.name,
            total_review_count=listing_stats.get("reviewsCount"),
            average_rating=listing_stats.get("totalScore"),
            notes=notes,
            resolved_url=resolved_url,
            resolved_name=resolved_name,
        )

    # ------------------------------------------------------------------
    def _await_listing_stats(self, page) -> bool:
        """Wait until the listing's rating and review count are readable.

        Returns whether they appeared. A False here is not fatal: the caller
        still verifies the listing and reads what it can, and the notes record
        that the figures were missing.
        """
        deadline = self.settings.listing_stats_wait_ms
        waited = 0
        step = 250
        while waited < deadline:
            try:
                stats = page.evaluate(LISTING_STATS_SCRIPT) or {}
                if stats.get("reviewsCount") is not None:
                    return True
            except Exception:
                pass  # the page is still assembling itself
            page.wait_for_timeout(step)
            waited += step
        return False

    @staticmethod
    def _read_listing_stats(page, notes: List[str]) -> dict:
        """The listing's own star average and lifetime review count."""
        try:
            stats = page.evaluate(LISTING_STATS_SCRIPT) or {}
        except Exception as exc:
            notes.append(f"Could not read the listing's headline rating ({exc}).")
            return {}
        if stats.get("totalScore") is None:
            notes.append("Listing headline rating not found on the page.")
        return stats

    # ------------------------------------------------------------------
    def _warm_session(self, page, notes: List[str]) -> None:
        """Browse to Google and Maps before opening the listing.

        Skipping this makes Google serve the reduced, reviews-free listing most
        of the time. Failure here is not fatal -- we still try the listing.
        """
        for url in WARMUP_URLS:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2200)
                if "/sorry/" in page.url:
                    raise AccessBlocked(
                        "Google served a CAPTCHA during session warm-up. Not attempting to "
                        "bypass it. Reduce CHECK_INTERVAL frequency, or use COLLECTOR_BACKEND=gbp_api."
                    )
            except AccessBlocked:
                raise
            except Exception as exc:
                notes.append(f"Session warm-up step {url} failed ({exc}); continuing anyway.")

    # ------------------------------------------------------------------
    def _verify_listing(self, page, business, notes: List[str]) -> None:
        """Guard against ever monitoring a similarly named business."""
        try:
            heading = page.locator("h1").first.inner_text(timeout=10000).strip()
        except Exception:
            notes.append("Could not read the listing heading to verify identity.")
            return

        expected = business.name.lower().replace("-", " ")
        actual = heading.lower().replace("-", " ")
        if expected not in actual and actual not in expected:
            raise AccessBlocked(
                f"Identity check failed for {business.name}: the page that loaded is titled "
                f"{heading!r}. Refusing to collect reviews from a different business. "
                "Check the configured PLACE_ID / GOOGLE_URL."
            )

    # ------------------------------------------------------------------
    def _open_reviews(self, page, business, notes: List[str]) -> None:
        """Open the full reviews list, or explain precisely why we cannot.

        Google serves two different layouts to signed-out clients, seemingly at
        random per session:

          A) the full listing, with a "Reviews for <business>" tab and a sort
             control -- everything we need;
          B) a reduced listing with only Overview/About tabs and no reviews at
             all.

        We take (A) when offered, fall back to whatever review cards the
        Overview panel already carries, and only give up when there is genuinely
        nothing to read.
        """
        tab_selectors = [
            "button[role='tab'][aria-label*='Reviews' i]",
            "button[aria-label^='Reviews for' i]",
            "button[jsaction*='moreReviews']",
        ]
        for selector in tab_selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0 and locator.is_visible():
                    locator.click(timeout=8000)
                    page.wait_for_timeout(3500)
                    self._sort_by_newest(page, notes)
                    return
            except Exception:
                continue

        # No reviews tab. The Overview panel sometimes still carries a handful
        # of review cards -- usable, but ordered by relevance rather than date.
        if page.locator("div[data-review-id]").count() > 0:
            notes.append(
                "No reviews tab was offered; read the Overview preview cards instead. "
                "These are ordered by relevance, not date, so a very new review may not "
                "appear until Google serves the full listing."
            )
            return

        raise AccessBlocked(
            f"{business.name}: Google served the signed-out reduced listing -- no reviews tab "
            "and no review cards. This is a Google-side restriction on anonymous access; it is "
            "deliberately not worked around (no sign-in, no CAPTCHA solving, no evasion). "
            "Session warm-up normally prevents this -- check PLAYWRIGHT_WARM_SESSION is true. "
            "It is also intermittent, so the next scheduled check may well succeed. For "
            "collection that never depends on chance, use COLLECTOR_BACKEND=gbp_api -- free, "
            "supported, complete data. See the README, 'Google access reality check'."
        )

    # ------------------------------------------------------------------
    def _sort_by_newest(self, page, notes: List[str]) -> None:
        """Order the list newest-first.

        Google's default is "Most relevant", which can bury a brand-new review
        below older ones -- fatal for a *new review* collector.

        Best effort: if anything goes wrong we make certain the menu is closed
        before returning. A menu left open sits over the review pane and stops
        it scrolling, which silently caps collection at the first page.
        """
        menu_opened = False
        try:
            sort_button = page.locator(
                "button[aria-label='Sort reviews' i], button[aria-label*='Sort' i]"
            ).first
            if sort_button.count() == 0:
                notes.append("No sort control found; list left in Google's default order.")
                return

            sort_button.click(timeout=6000)
            menu_opened = True

            # Wait for the menu to render rather than guessing at a delay.
            try:
                page.wait_for_selector("[role='menuitemradio'], [role='menuitem']", timeout=8000)
            except Exception:
                notes.append("Sort menu did not open in time; using Google's default order.")
                return

            items = page.locator("[role='menuitemradio'], [role='menuitem']")
            labels = []
            for index in range(items.count()):
                item = items.nth(index)
                try:
                    label = (item.inner_text() or "").strip()
                except Exception:
                    continue
                labels.append(label)
                if "newest" in label.lower():
                    item.click(timeout=5000)
                    menu_opened = False
                    page.wait_for_timeout(4000)
                    return

            notes.append(
                f"Sort menu had no 'Newest' option (saw: {labels or 'nothing'}); "
                "using Google's default order."
            )
        except Exception as exc:
            notes.append(f"Could not sort by newest ({exc}); using Google's default order.")
        finally:
            # Unconditionally, not just when menu_opened is True. A click that
            # times out on "waiting for scheduled navigations to finish" has
            # already opened the menu, and the exception fires before
            # menu_opened is set -- so the flag reads False while a menu is
            # sitting on screen. That menu covers the review pane, scrolling
            # does nothing, and collection silently caps at the first page
            # (~20 reviews) with no error. This is safe and cheap either way:
            # _dismiss_menu returns as soon as it sees no menu.
            self._dismiss_menu(page)

    # ------------------------------------------------------------------
    @staticmethod
    def _dismiss_menu(page) -> None:
        """Close any open menu so it cannot block scrolling of the review pane."""
        for attempt in (1, 2):
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
                if page.locator("[role='menuitemradio'], [role='menuitem']").count() == 0:
                    return
                if attempt == 2:
                    # Escape did not take; click a neutral spot instead.
                    page.mouse.click(700, 40)
                    page.wait_for_timeout(500)
            except Exception:
                return

    # ------------------------------------------------------------------
    def _scroll_and_extract(self, page, limit: int) -> List[dict]:
        """Scroll the lazy-loading reviews pane until we have enough reviews."""
        cards: List[dict] = []
        stagnant_rounds = 0

        for _ in range(max(1, self.settings.playwright_scroll_rounds)):
            try:
                cards = page.evaluate(EXTRACT_SCRIPT)
            except Exception as exc:
                raise ParseError(f"In-page extraction failed: {exc}") from exc

            if len(cards) >= limit:
                break

            previous = len(cards)
            # Scroll the review pane itself, not the window.
            page.evaluate(
                """() => {
                    const panes = Array.from(document.querySelectorAll('div[role="main"] div'))
                        .filter(d => d.scrollHeight > d.clientHeight + 200);
                    const pane = panes.sort((a,b) => b.scrollHeight - a.scrollHeight)[0];
                    if (pane) pane.scrollTop = pane.scrollHeight;
                    else window.scrollTo(0, document.body.scrollHeight);
                }"""
            )
            page.wait_for_timeout(1600)

            try:
                after = len(page.evaluate(EXTRACT_SCRIPT))
            except Exception:
                after = previous
            stagnant_rounds = stagnant_rounds + 1 if after <= previous else 0
            if stagnant_rounds >= 3:
                break  # Reached the end of the list.

        # Expand truncated review bodies so we store the full text.
        try:
            more_buttons = page.locator("button[aria-label='See more' i], button.w8nwRe")
            for index in range(min(more_buttons.count(), limit)):
                try:
                    more_buttons.nth(index).click(timeout=1500)
                except Exception:
                    continue
            page.wait_for_timeout(700)
            cards = page.evaluate(EXTRACT_SCRIPT)
        except Exception:
            pass

        return cards[:limit] if limit else cards
