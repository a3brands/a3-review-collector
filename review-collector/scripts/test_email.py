#!/usr/bin/env python3
"""Send one real test notification and report exactly what happened.

    python scripts/test_email.py

Run this before trusting the notifications. It reports configuration problems
in plain language rather than a raw SMTP traceback.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.services import notifier, stats  # noqa: E402


def main() -> None:
    settings = get_settings()

    print("Configuration")
    print("-" * 60)
    print(f"  NOTIFY_ENABLED        : {settings.notify_enabled}")
    print(f"  SMTP_HOST / PORT      : {settings.smtp_host}:{settings.smtp_port}")
    print(f"  SMTP_USERNAME         : {settings.smtp_username or '(not set)'}")
    print(f"  SMTP_PASSWORD         : {'set (' + str(len(settings.smtp_password)) + ' chars)' if settings.smtp_password else '(not set)'}")
    print(f"  From                  : {settings.resolved_notify_from() or '(not set)'}")
    print(f"  To                    : {', '.join(settings.notify_recipients()) or '(not set)'}")
    print(f"  On Check Now          : {settings.notify_on_check_now}")
    print(f"  On manual Refresh     : {settings.notify_on_refresh}")
    print(f"  Rate limit            : {settings.notify_min_interval_seconds}s per event type")
    print()

    problem = settings.notify_config_problem()
    if problem:
        raise SystemExit(f"Not ready to send:\n  {problem}\n\nSee README, 'Email notifications'.")

    if len(settings.smtp_password.replace(" ", "")) != 16 and "gmail" in settings.smtp_host:
        print(
            "  NOTE: Gmail App Passwords are 16 characters. Yours is "
            f"{len(settings.smtp_password.replace(' ', ''))}. If the send fails with an\n"
            "        authentication error, that is almost certainly why.\n"
        )

    print("Sending a test message (this blocks until Gmail accepts it)...")
    notifier.reset_rate_limit()

    # Use the REAL current state, so the test message looks exactly like a live
    # one. Hard-coded sample numbers previously contradicted each other -- the
    # body said "nothing waiting" while the totals said 3.
    try:
        totals = stats.global_stats()
        pending = stats.pending_reviews(20)
        results = [
            {
                "business_name": business["name"],
                "status": "success",
                "reviews_found": (business.get("last_check") or {}).get("reviews_found", 0),
                "new_reviews": (business.get("last_check") or {}).get("new_reviews", 0),
                "skipped_existing": (business.get("last_check") or {}).get("skipped_existing", 0),
            }
            for business in stats.business_stats()
        ]
    except Exception as exc:
        print(f"  (could not read live stats: {exc}; sending with empty data)")
        totals, pending, results = {}, [], []

    result = notifier.send_event(
        "check_now",
        actor="scripts/test_email.py",
        results=results,
        totals=totals,
        reviews=pending,
        blocking=True,
    )

    print()
    if result.get("sent"):
        print("SENT. Subject:", result.get("subject"))
        print()
        print("Now check the Gmail inbox. If it landed in Spam, add the filter described")
        print("in README 'Email notifications' -- that is the only way to guarantee the")
        print("Inbox, and it takes about 30 seconds to set up.")
    else:
        raise SystemExit(f"NOT SENT: {result.get('reason')}")


if __name__ == "__main__":
    main()
