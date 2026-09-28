#!/usr/bin/env python3
"""Check the A3 connection before trusting it with 583 reviews.

    python scripts/a3_sync_dryrun.py            # show what WOULD be sent
    python scripts/a3_sync_dryrun.py --send 5   # really send 5, then report

The --send form is the important one. A3 replies with created / updated /
unchanged per review, which answers the question no amount of code reading can:
do our Google review ids match the ones A3 already holds from Apify? If they do,
you will see 'updated' or 'unchanged'. A wall of 'created' means we are about to
duplicate a review A3 already has -- stop and reconcile the ids first.
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.services import a3_sync, stats  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", type=int, default=0,
                        help="actually send this many reviews (default: dry run only)")
    parser.add_argument("--mark", action="store_true",
                        help="record accepted reviews as synced (default: do not)")
    args = parser.parse_args()

    settings = get_settings()

    print("A3 sync configuration")
    print("-" * 62)
    print(f"  enabled            : {settings.a3_sync_enabled}")
    print(f"  url                : {settings.a3_sync_url or '(not set)'}")
    print(f"  secret             : {'set, ' + str(len(settings.a3_sync_secret)) + ' chars' if settings.a3_sync_secret else '(not set)'}")
    print(f"  dealer mapping     : bmw_fwb -> {settings.a3_dealer_id('bmw_fwb')}, "
          f"mb_fwb -> {settings.a3_dealer_id('mb_fwb')}")
    print(f"  send reply state   : {settings.a3_sync_send_reply_state}"
          f"{'' if settings.a3_sync_send_reply_state else '  (off: protects A3 business_reply)'}")
    print()

    problem = settings.a3_sync_problem()
    if problem:
        raise SystemExit(f"Not ready: {problem}")

    pending = stats.unsynced_reviews(limit=max(args.send, 20))
    print(f"Reviews A3 has never accepted: {len(pending)}"
          f"{'' if len(pending) < 20 else '+ (showing the first 20)'}\n")
    if not pending:
        print("Nothing to send.")
        return

    for review in pending[: args.send or 5]:
        print(f"  {review['rating']}* {review['reviewer_name'][:24]:<24} "
              f"{review['business_key']:<8} google_id={review['google_review_id'][:34]}")
    print()

    if not args.send:
        print("Dry run only. Re-run with --send 5 to test the live connection.")
        return

    batch = pending[: args.send]
    print(f"Sending {len(batch)} review(s) to A3...\n")
    outcome = a3_sync.push(batch, settings=settings)

    actions = collections.Counter(r.get("action", "?") for r in outcome["results"])
    print("A3 replied:")
    for action, count in actions.items():
        print(f"  {action:<10} {count}")
    for entry in outcome["results"]:
        if entry.get("action") == "error":
            print(f"    error: {entry.get('error')}")
    print()

    if actions.get("created") and not (actions.get("updated") or actions.get("unchanged")):
        print("WARNING: every review came back 'created'.")
        print("  If A3 already holds these reviews from Apify, the ids do not match and")
        print("  a full sync would DUPLICATE them. Reconcile before syncing everything.")

    if args.mark:
        stamped = stats.mark_a3_synced(outcome["accepted"])
        print(f"Marked {stamped} review(s) as synced.")
    else:
        print("Not marking anything as synced (pass --mark to record it).")


if __name__ == "__main__":
    main()
