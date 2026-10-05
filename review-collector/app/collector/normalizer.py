"""RawReview -> database row.

Kept separate from the backends so that both the GBP API and the Maps scraper
produce identical rows, and so the storage shape can change without touching
any Google-specific code.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from app.collector.base import RawReview
from app.collector.parser import google_review_permalink
from app.database.models import Business, Review, utcnow

MAX_TEXT_CHARS = 20000
MAX_NAME_CHARS = 255


def _truncate(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return value[:limit]


def to_review_row(
    business: Business,
    review_id: str,
    review_id_is_native: bool,
    raw: RawReview,
    *,
    processed: bool = False,
    detected_at: Optional[dt.datetime] = None,
) -> Review:
    """Build (but do not add) a Review row.

    Missing information stays missing -- nothing is defaulted to a fake value.
    """
    rating = raw.rating
    if rating is not None and not (1 <= rating <= 5):
        rating = None

    # Applied here rather than in a backend parser so every path gets it: a link
    # to the dealership's whole review list means scrolling to find the review
    # you just clicked. Falls back to whatever the source gave us when there is
    # no native Google id to build one from.
    native_id = review_id.split(":", 1)[1] if (review_id_is_native and ":" in review_id) else None
    review_url = google_review_permalink(
        native_id,
        feature_id=getattr(business, "feature_id", None),
        latitude=getattr(business, "latitude", None),
        longitude=getattr(business, "longitude", None),
    ) or raw.review_url or None

    now = detected_at or utcnow()
    return Review(
        business_id=business.id,
        review_id=review_id,
        review_id_is_native=review_id_is_native,
        reviewer_name=_truncate(raw.reviewer_name, MAX_NAME_CHARS),
        reviewer_profile_url=raw.reviewer_profile_url or None,
        rating=rating,
        review_text=_truncate(raw.review_text, MAX_TEXT_CHARS),
        review_date=raw.review_date,
        review_date_is_approximate=bool(raw.review_date_is_approximate),
        review_url=review_url,
        owner_replied=bool(raw.owner_replied),
        owner_reply=_truncate(raw.owner_reply, MAX_TEXT_CHARS),
        owner_reply_date=raw.owner_reply_date,
        source=raw.source or "unknown",
        detected_at=now,
        processed=processed,
        processed_at=now if processed else None,
    )


def rebuild_review_links(session, business: Business) -> int:
    """Rewrite this dealership's stored review links that are not the right one.

    A link is built once, when the review is first stored, and never again. So a
    dealership whose listing id arrived late kept links that open Google with no
    dealership name (`0x0:0x0`) forever. Run after every check, this repairs
    them as soon as the id is known. Returns how many changed.
    """
    if not business.feature_id:
        return 0
    from sqlalchemy import or_

    changed = 0
    stale = session.query(Review).filter(
        Review.business_id == business.id,
        Review.review_id_is_native.is_(True),
        or_(Review.review_url.is_(None),
            ~Review.review_url.contains(business.feature_id)),
    )
    for review in stale:
        native_id = review.review_id.split(":", 1)[1] if ":" in review.review_id else None
        url = google_review_permalink(
            native_id,
            feature_id=business.feature_id,
            latitude=business.latitude,
            longitude=business.longitude,
        )
        if url and url != review.review_url:
            review.review_url = url
            changed += 1
    return changed
