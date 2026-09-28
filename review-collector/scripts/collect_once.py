#!/usr/bin/env python3
"""Run exactly one collection cycle, print the result, and exit.

    python scripts/collect_once.py

The service normally collects from inside the long-running API process, on
APScheduler's fifteen minute interval. That is the right design for production
and the wrong one for answering "is the scraper working on this machine?" --
you cannot tell a scheduler that is about to fail from one that is merely
waiting.

So this exists for three moments:

  1. Immediately after provisioning a new server, to prove Chromium can reach
     Google from that host before anything is cut over to it.
  2. When a check has failed and you want the error in front of you now rather
     than at the top of the next quarter hour.
  3. As a cron fallback, if the in-process scheduler is ever taken out. It takes
     the same cycle lock as the scheduled job, so a cron run that overlaps a
     scheduled one exits rather than collecting twice.

It shares the database, the deduplication and the notification path with the
scheduled run. A review found here is stored, emailed and synced exactly as if
the scheduler had found it -- and, just as importantly, a review already on file
is NOT re-emailed.

--dry-run suppresses the email and the sync but still stores. That is
deliberate: skipping the write would leave the review looking new to the next
scheduled run, and the whole point of the exercise is that nobody is emailed
twice.

Exit codes, so a cron wrapper or a provisioning script can branch on them:
    0  every dealership checked successfully
    1  at least one dealership failed (the others still ran)
    2  the cycle could not start at all
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings, reset_settings_cache  # noqa: E402
from app.database.database import init_db, session_scope  # noqa: E402
from app.database.models import CheckRun, Review, utcnow  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.services.collection_service import CollectionService  # noqa: E402


def _fmt_duration(ms) -> str:
    if not ms:
        return "-"
    return f"{ms / 1000:.1f}s"


def _report(since):
    """The per-dealership summary, read back from what the cycle recorded.

    Deliberately not built from the dict run_cycle returns. That dict is the
    scheduler's summary and carries neither the fast-path flag nor the number of
    emails sent, so printing it would mean reporting "Emails: -" on a run that
    had just sent three. CheckRun and Review.notified_at are where those facts
    actually live, and reading them back also proves the write landed.
    """
    with session_scope() as session:
        runs = (
            session.query(CheckRun)
            .filter(CheckRun.started_at >= since)
            .order_by(CheckRun.started_at)
            .all()
        )
        emailed = {}
        rows = (
            session.query(Review.business_id)
            .filter(Review.notified_at.isnot(None), Review.notified_at >= since)
            .all()
        )
        for (business_id,) in rows:
            emailed[business_id] = emailed.get(business_id, 0) + 1

        return [
            {
                "name": run.business.name if run.business else f"business {run.business_id}",
                "found": run.reviews_found,
                "new": run.new_reviews,
                "duplicates": run.skipped_existing,
                "emails": emailed.get(run.business_id, 0),
                "backend": run.backend,
                "fast_path": bool(run.fast_path),
                "duration_ms": run.duration_ms,
                "status": run.status,
                "error": run.error_message,
                "will_retry": run.will_retry,
                "started_at": run.started_at,
            }
            for run in runs
        ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one A3 review collection cycle and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check Google and store what is found, but email nobody and sync "
             "nowhere. Use on a new server to prove the scrape works without "
             "notifying anyone.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the per-dealership log lines; print only the summary.",
    )
    args = parser.parse_args()

    # Applied to the ENVIRONMENT, before Settings is ever constructed, and not
    # to a copy of the settings object.
    #
    # A copy would not have worked. CollectionService holds the settings it is
    # given, but notifier.send_event() is called without one and falls back to
    # the lru_cached get_settings() -- so a dry run built by copying would have
    # checked Google, printed "DRY RUN", and emailed everybody anyway. Setting
    # the environment first is the only version of this that is true for every
    # code path, including the ones that ask for settings themselves.
    if args.dry_run:
        os.environ["NOTIFY_ENABLED"] = "false"
        os.environ["A3_SYNC_ENABLED"] = "false"
        os.environ["GRM_ENABLED"] = "false"
    if args.quiet:
        os.environ["LOG_LEVEL"] = "WARNING"
    reset_settings_cache()

    settings = get_settings()
    setup_logging(settings)

    # The scheduled path does this at API startup. A one-shot run has no
    # startup, and a fresh server has no tables yet.
    init_db()

    if args.dry_run:
        # Belt and braces: if any of the three failed to take, say so and stop
        # rather than "dry running" straight into a customer's inbox.
        live = [
            n for n, v in (
                ("NOTIFY_ENABLED", settings.notify_enabled),
                ("A3_SYNC_ENABLED", settings.a3_sync_enabled),
                ("GRM_ENABLED", settings.grm_enabled),
            ) if v
        ]
        if live:
            print(f"Refusing to dry run: {', '.join(live)} is still on.", file=sys.stderr)
            return 2
        print(
            "DRY RUN: Google will be checked and new reviews WILL be stored, "
            "but no email is sent and nothing is synced to the Responder.\n"
            "(Storing is left on so deduplication stays honest -- a review "
            "found here must not be emailed twice later.)\n"
        )

    # Taken before the cycle so the report picks up exactly the CheckRun rows
    # this run produced, and none from the scheduled run that may have finished
    # a moment earlier.
    since = utcnow()

    service = CollectionService(settings)
    result = service.run_cycle(trigger="manual" if not args.dry_run else "dry-run")

    if result.get("skipped"):
        print(f"Did not run: {result.get('reason')}")
        return 2

    report = _report(since)
    failed = [r for r in report if r["status"] == "failed"]

    print()
    for r in report:
        stamp = r["started_at"].strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{stamp} UTC]")
        print(f"  {r['name']}")
        print(f"  Found:      {r['found']}")
        print(f"  New:        {r['new']}")
        print(f"  Duplicates: {r['duplicates']}")
        print(f"  Emails:     {r['emails']}")
        print(f"  Backend:    {r['backend'] or '-'}"
              f"{'  (fast path, review pane not opened)' if r['fast_path'] else ''}")
        print(f"  Duration:   {_fmt_duration(r['duration_ms'])}")
        print(f"  Status:     {r['status'].upper()}")
        if r["error"]:
            print(f"  Error:      {r['error']}")
            print(f"  Retry:      {'yes, next scheduled run' if r['will_retry'] else 'no'}")
        print()

    print(
        f"Cycle complete: {sum(r['new'] for r in report)} new review(s), "
        f"{sum(r['emails'] for r in report)} email(s) sent; "
        f"{len(report) - len(failed)}/{len(report)} dealership(s) succeeded."
    )
    if failed:
        print("Failed: " + ", ".join(r["name"] for r in failed))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
