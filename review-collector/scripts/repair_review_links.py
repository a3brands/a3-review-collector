#!/usr/bin/env python3
"""Rebuild Google review links that open without the dealership's name.

A review link carries the listing's feature id. Dealerships whose feature id
was never stored got `0x0:0x0` instead, and Google opens those reviews on a
bare page with no dealership name. This reads the feature id and map position
from each dealership's own Google URL, stores them, and rebuilds the links.

Only the link changes. Review text, dates, processed state and ids are left
alone. The next sync to the reviews manager carries the new links across.

    python scripts/repair_review_links.py            # show what would change
    python scripts/repair_review_links.py --apply    # write the corrections
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collector.normalizer import rebuild_review_links  # noqa: E402
from app.collector.parser import listing_identity  # noqa: E402
from app.database.database import init_db, session_scope  # noqa: E402
from app.database.models import Business  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the corrections")
    args = parser.parse_args()

    init_db()
    total = 0
    with session_scope() as session:
        for business in session.query(Business).order_by(Business.id):
            if not business.feature_id or business.latitude is None:
                for url in (business.google_url, business.configured_url):
                    found = listing_identity(url)
                    if found["feature_id"]:
                        business.feature_id = business.feature_id or found["feature_id"]
                        if business.latitude is None and found["latitude"] is not None:
                            business.latitude = found["latitude"]
                            business.longitude = found["longitude"]
                        break

            if not business.feature_id:
                print(f"{business.name}: no feature id in its Google URL, left as is")
                continue

            changed = rebuild_review_links(session, business)
            total += changed
            print(f"{business.name}: {business.feature_id}, {changed} link(s) to rebuild")

        if not args.apply:
            session.rollback()

    print(f"\n{total} link(s) {'rebuilt' if args.apply else 'would be rebuilt (dry run, use --apply)'}")


if __name__ == "__main__":
    main()
