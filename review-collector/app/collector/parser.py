"""Pure parsing helpers -- no network, no browser, fully unit-testable.

If Google changes its markup or its API shape, this file (plus the extraction
selectors in google_maps.py) is what needs updating. Nothing else.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional

# ---------------------------------------------------------------------------
# Star ratings
# ---------------------------------------------------------------------------

# The Business Profile API returns an enum, not a number.
GBP_STAR_ENUM = {
    "ONE": 1,
    "TWO": 2,
    "THREE": 3,
    "FOUR": 4,
    "FIVE": 5,
    "STAR_RATING_UNSPECIFIED": None,
}

_STAR_LABEL_RE = re.compile(r"([0-9](?:[.,][0-9])?)\s*star", re.IGNORECASE)


def parse_star_rating(value) -> Optional[int]:
    """Accept the GBP enum, a number, or an aria-label like '4 stars'.

    Returns an int 1-5, or None when the rating genuinely is not available.
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        rating = int(round(float(value)))
        return rating if 1 <= rating <= 5 else None

    text = str(value).strip()
    if not text:
        return None

    upper = text.upper()
    if upper in GBP_STAR_ENUM:
        return GBP_STAR_ENUM[upper]

    match = _STAR_LABEL_RE.search(text)
    if match:
        rating = int(round(float(match.group(1).replace(",", "."))))
        return rating if 1 <= rating <= 5 else None

    if text.isdigit():
        rating = int(text)
        return rating if 1 <= rating <= 5 else None

    return None


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_RELATIVE_RE = re.compile(
    r"\b(?:(a|an|\d+)\s+)?(second|minute|hour|day|week|month|year)s?\s+ago\b",
    re.IGNORECASE,
)

_UNIT_DAYS = {
    "second": 1 / 86400,
    "minute": 1 / 1440,
    "hour": 1 / 24,
    "day": 1.0,
    "week": 7.0,
    "month": 30.44,
    "year": 365.25,
}


def parse_absolute_date(value: Optional[str]) -> Optional[dt.datetime]:
    """Parse an RFC3339 / ISO-8601 timestamp such as the GBP API createTime."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Python's fromisoformat dislikes 'Z' and sub-second precision beyond 6 digits.
    text = text.replace("Z", "+00:00")
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def parse_relative_date(value: Optional[str], now: Optional[dt.datetime] = None):
    """Turn 'a week ago' / '3 months ago' into (approx_datetime, True).

    Returns (None, False) when the string is not a recognised relative date.
    The second element is the "this is approximate" flag which we carry all the
    way to the API so nobody mistakes it for a precise timestamp.
    """
    if not value:
        return None, False
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    match = _RELATIVE_RE.search(str(value))
    if not match:
        return None, False
    qty_raw, unit = match.group(1), match.group(2).lower()
    if qty_raw is None or qty_raw.lower() in {"a", "an"}:
        qty = 1.0
    else:
        try:
            qty = float(qty_raw)
        except ValueError:
            return None, False
    return now - dt.timedelta(days=qty * _UNIT_DAYS[unit]), True


def parse_review_date(value: Optional[str], now: Optional[dt.datetime] = None):
    """Best-effort date parse. Returns (datetime|None, is_approximate)."""
    absolute = parse_absolute_date(value)
    if absolute is not None:
        return absolute, False
    return parse_relative_date(value, now=now)


# ---------------------------------------------------------------------------
# Text cleanup
# ---------------------------------------------------------------------------

_TRAILING_MORE_RE = re.compile(r"\s*(?:…|\.\.\.)?\s*(?:More|Read more|Show more)\s*$", re.IGNORECASE)

# The dealership's own reply must never end up in the customer's comment. The
# extractor already scopes to the customer's node; this is the second line of
# defence for both backends.
_OWNER_REPLY_RE = re.compile(r"Response from the owner", re.IGNORECASE)


def clean_review_text(value: Optional[str]) -> Optional[str]:
    """Normalise whitespace and strip the trailing 'More' expander label.

    Returns None for an empty review -- a rating with no comment is normal and
    must not become an empty string pretending to be a review body.
    """
    if value is None:
        return None
    text = str(value).replace(" ", " ")
    marker = _OWNER_REPLY_RE.search(text)
    if marker:
        text = text[: marker.start()]
    text = _TRAILING_MORE_RE.sub("", text)
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line).strip()
    return text or None


def clean_owner_reply(value: Optional[str]) -> Optional[str]:
    """Tidy the dealership's own reply.

    Unlike clean_review_text this does NOT stop at the owner marker -- here the
    reply IS the content. Kept separate so the two can never be confused.
    """
    if value is None:
        return None
    text = str(value).replace("\u00a0", " ")
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line).strip()
    return text[:20000] or None


def clean_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    name = re.sub(r"\s+", " ", str(value)).strip()
    return name or None


# ---------------------------------------------------------------------------
# Google Business Profile API review objects
# ---------------------------------------------------------------------------

def google_review_permalink(
    native_review_id: Optional[str],
    *,
    feature_id: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> Optional[str]:
    """A link that opens this review ON the dealership's Google listing.

    Google publishes no documented per-review URL, but its Maps review
    permalink carries the review id and the listing's feature id inside the
    `data=` protobuf.

    The feature id matters. Without it (`0x0:0x0`) the review opens as a bare
    page with no dealership name, no map pin and no listing context -- it shows
    the review but gives no sense of which business it belongs to. With it, the
    listing panel opens with the review shown against it.

    Returns None without a native Google id: a locally generated fingerprint
    would build a link that goes nowhere, which is worse than no link.
    """
    if not native_review_id:
        return None

    place = feature_id or "0x0:0x0"
    if latitude is not None and longitude is not None:
        centre = f"@{latitude},{longitude},17z/"
    else:
        centre = ""

    return (
        f"https://www.google.com/maps/reviews/{centre}"
        "data=!3m1!4b1!4m6!14m5!1m4!2m3!1s"
        f"{native_review_id}!2m1!1s{place}?hl=en"
    )


def parse_gbp_review(payload: dict, *, place_id: Optional[str] = None) -> dict:
    """Map one v4 GBP API review object onto RawReview keyword arguments."""
    reviewer = payload.get("reviewer") or {}
    reply = payload.get("reviewReply") or {}
    review_date, approximate = parse_review_date(
        payload.get("createTime") or payload.get("updateTime")
    )
    review_id = payload.get("reviewId") or payload.get("name", "").rsplit("/", 1)[-1] or None

    review_url = google_review_permalink(review_id) or payload.get("reviewReplyUrl")
    if not review_url and place_id and review_id:
        # Deep link back into the public listing so A3 can open the review.
        review_url = f"https://search.google.com/local/reviews?placeid={place_id}"

    return {
        "source_review_id": review_id,
        "reviewer_name": clean_name(reviewer.get("displayName")),
        "reviewer_profile_url": reviewer.get("profilePhotoUrl"),
        "rating": parse_star_rating(payload.get("starRating")),
        "review_text": clean_review_text(payload.get("comment")),
        "review_date": review_date,
        "review_date_is_approximate": approximate,
        "review_date_raw": payload.get("createTime") or payload.get("updateTime"),
        "review_url": review_url,
        "owner_replied": bool(reply.get("comment") or reply.get("updateTime")),
        "owner_reply": clean_owner_reply(reply.get("comment")),
        "owner_reply_date": parse_absolute_date(reply.get("updateTime")),
        "source": "gbp_api",
    }


# ---------------------------------------------------------------------------
# Google Maps DOM review cards
# ---------------------------------------------------------------------------

def parse_maps_card(card: dict, *, place_id: Optional[str] = None) -> dict:
    """Map one scraped Maps review card dict onto RawReview keyword arguments.

    ``card`` is the plain dict produced by the in-page extraction script in
    google_maps.py, so this stays testable without a browser.
    """
    review_date, approximate = parse_review_date(card.get("date_text"))
    review_id = card.get("review_id") or None

    # The per-review permalink wins over anything the page gave us: a
    # place-level link means scrolling a listing to find this review again.
    review_url = google_review_permalink(review_id) or card.get("review_url")
    if not review_url and place_id:
        review_url = f"https://search.google.com/local/reviews?placeid={place_id}"

    return {
        "source_review_id": review_id,
        "reviewer_name": clean_name(card.get("reviewer_name")),
        "reviewer_profile_url": card.get("reviewer_profile_url") or None,
        "rating": parse_star_rating(card.get("rating_label") or card.get("rating")),
        "review_text": clean_review_text(card.get("text")),
        "review_date": review_date,
        "review_date_is_approximate": approximate,
        "review_date_raw": card.get("date_text"),
        "review_url": review_url,
        "owner_replied": bool(card.get("owner_replied")),
        "owner_reply": clean_owner_reply(card.get("owner_reply")),
        "owner_reply_date": parse_review_date(card.get("owner_reply_date_text"))[0],
        "source": "google_maps",
    }
