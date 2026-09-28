"""New-review detection.

Two layers of protection, because sending A3 the same review twice is the
single worst failure mode of this service:

1. A stable ``review_id`` computed here (Google's own ID whenever available).
2. A UNIQUE constraint on ``reviews.review_id`` in SQLite, so even a race
   between a scheduled run and a manual "Check Now" cannot create a duplicate.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Iterable, Optional, Set

from sqlalchemy.orm import Session

from app.collector.base import RawReview
from app.database.models import Review

_WS_RE = re.compile(r"\s+")


def _norm(value: Optional[str]) -> str:
    if not value:
        return ""
    return _WS_RE.sub(" ", str(value)).strip().lower()


def build_review_id(business_key: str, raw: RawReview) -> tuple[str, bool]:
    """Return (review_id, is_native).

    Native IDs are namespaced by business key so two dealerships can never
    collide, and so the value stays globally unique for the
    ``POST /api/reviews/{review_id}/processed`` route.
    """
    if raw.source_review_id:
        return f"{business_key}:{raw.source_review_id}", True

    # No stable ID from Google -- build a deterministic fingerprint from the
    # parts of a review that do not change once it is posted.
    #
    # The date is bucketed to the day: relative dates ("2 weeks ago") drift
    # slightly between checks, and an un-bucketed timestamp would make the same
    # review fingerprint differently on every run.
    day = ""
    if raw.review_date is not None:
        day = raw.review_date.date().isoformat()

    parts = [
        business_key,
        _norm(raw.reviewer_name),
        str(raw.rating if raw.rating is not None else ""),
        _norm(raw.review_text)[:600],
        day if not raw.review_date_is_approximate else "",
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:40]
    return f"{business_key}:fp_{digest}", False


def existing_review_ids(session: Session, review_ids: Iterable[str]) -> Set[str]:
    """One query for the whole batch instead of one per review."""
    ids = [rid for rid in review_ids if rid]
    if not ids:
        return set()
    found: Set[str] = set()
    # SQLite caps variables per statement; chunk to stay well under it.
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        rows = session.query(Review.review_id).filter(Review.review_id.in_(chunk)).all()
        found.update(row[0] for row in rows)
    return found


def review_exists(session: Session, review_id: str) -> bool:
    return session.query(Review.id).filter(Review.review_id == review_id).first() is not None


def partition_new(session: Session, business_key: str, raws: Iterable[RawReview]):
    """Split collected reviews into (new, already_seen).

    Also de-duplicates *within* the batch, which matters when Google renders the
    same review twice while a lazy-loading list is being scrolled.
    """
    raws = list(raws)
    annotated = []
    for raw in raws:
        review_id, is_native = build_review_id(business_key, raw)
        annotated.append((review_id, is_native, raw))

    known = existing_review_ids(session, (rid for rid, _, _ in annotated))

    new_items = []
    seen_items = []
    batch_seen: Set[str] = set()
    for review_id, is_native, raw in annotated:
        if review_id in known or review_id in batch_seen:
            seen_items.append((review_id, is_native, raw))
            continue
        batch_seen.add(review_id)
        new_items.append((review_id, is_native, raw))
    return new_items, seen_items
