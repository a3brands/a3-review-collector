#!/usr/bin/env python3
"""Re-collect and correct the text of reviews already in the database.

Needed after a collector-side extraction bug: rows keep their identity,
processed state and detected_at, but their review_text / rating are refreshed
from Google. Nothing is deleted and nothing is re-queued to A3.

    python scripts/repair_reviews.py            # show what would change
    python scripts/repair_reviews.py --apply    # write the corrections
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collector.base import CollectorError  # noqa: E402
from app.collector.deduplication import build_review_id  # noqa: E402
from app.collector.registry import get_backends  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database.database import init_db, session_scope  # noqa: E402
from app.database.models import Business, Review  # noqa: E402

# An owner reply addresses the reviewer by first name and thanks them for the
# review. Requiring BOTH signals keeps genuine customer text ("Thank you for the
# great service") from being mistaken for one.
GRATITUDE_PHRASES = (
    "for taking the time to leave a review",
    "we appreciate your feedback",
    "we appreciate your support",
    "we truly appreciate your support",
    "thank you for choosing",
    "we appreciate your input",
    "your feedback is appreciated",
)


def looks_like_owner_reply(text: str, reviewer_name: str | None) -> bool:
    if not text:
        return False
    low = text.lower()
    if "response from the owner" in low:
        return True
    if not any(phrase in low for phrase in GRATITUDE_PHRASES):
        return False
    first_name = (reviewer_name or "").strip().split(" ")[0].lower()
    return bool(first_name) and first_name in low


def decide(row, fresh_text: str | None):
    """Return (should_update, why). Deliberately conservative.

    Never trades a longer stored comment for a shorter fresh read -- a failed
    'See more' expansion must not silently truncate real customer text.
    """
    stored = row.review_text or ""
    fresh = fresh_text or ""

    if stored and not fresh:
        if looks_like_owner_reply(stored, row.reviewer_name):
            return True, "stored text is the dealership's own reply -> clearing"
        return False, "fresh read returned nothing; keeping stored text"

    if looks_like_owner_reply(stored, row.reviewer_name) and fresh:
        return True, "stored text is the dealership's own reply -> replacing"

    if len(fresh) > len(stored):
        return True, "fresh text is more complete (stored was truncated)"

    return False, "no improvement"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--limit", type=int, default=400, help="reviews to re-read per business")
    args = parser.parse_args()

    settings = get_settings()
    init_db()

    backends = get_backends(settings)
    if not backends:
        raise SystemExit("No collector backend configured.")

    with session_scope() as session:
        businesses = session.query(Business).filter_by(active=True).all()
        targets = [(b.id, b.key, b.name) for b in businesses]

    total_changed = 0
    for business_id, key, name in targets:
        print(f"\n=== {name} ===")
        result = None
        for backend in backends:
            available, reason = backend.is_available()
            if not available:
                print(f"  backend {backend.name} unavailable: {reason}")
                continue
            try:
                with session_scope() as session:
                    business = session.get(Business, business_id)
                    session.expunge(business)
                result = backend.collect(business, args.limit)
                break
            except CollectorError as exc:
                print(f"  backend {backend.name} failed: {exc}")

        if result is None:
            print("  SKIPPED - could not re-collect. Run again later.")
            continue

        print(f"  re-read {len(result.reviews)} review(s) via {result.backend}")

        changed = 0
        skipped: list = []
        with session_scope() as session:
            for raw in result.reviews:
                review_id, _ = build_review_id(key, raw)
                row = session.query(Review).filter_by(review_id=review_id).one_or_none()
                if row is None:
                    continue  # genuinely new -- leave it to the normal collector

                fresh_text = (raw.review_text or None)
                if (row.review_text or None) == fresh_text and row.rating == raw.rating:
                    continue

                should_update, why = decide(row, fresh_text)
                if not should_update:
                    skipped.append((row.reviewer_name, why))
                    continue

                print(f"  - {row.reviewer_name or '(no name)'} [{row.rating}*]  ({why})")
                print(f"      stored  ({len(row.review_text or ''):4d}): {(row.review_text or '(none)')[:80]!r}")
                print(f"      correct ({len(fresh_text or ''):4d}): {(fresh_text or '(none)')[:80]!r}")
                changed += 1

                if args.apply:
                    row.review_text = fresh_text
                    if raw.rating is not None:
                        row.rating = raw.rating
                    # processed / processed_at / detected_at deliberately untouched

            if not args.apply:
                session.rollback()

        total_changed += changed
        if skipped:
            print(f"  {len(skipped)} row(s) deliberately left alone (e.g. {skipped[0][1]})")
        print(f"  {changed} row(s) {'corrected' if args.apply else 'would change'}")

    print(f"\n{total_changed} row(s) {'corrected' if args.apply else 'would change'}.")
    if not args.apply and total_changed:
        print("Re-run with --apply to write the corrections.")


if __name__ == "__main__":
    main()
