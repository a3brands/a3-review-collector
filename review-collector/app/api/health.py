"""Unauthenticated liveness endpoint.

Deliberately public so an uptime monitor can poll it, and deliberately free of
review content, business identifiers, API keys and error detail.

``status`` reflects whether the service is actually doing its job, not merely
whether the web process is up:

    ok        -- running, and the most recent check of every active dealership
                 succeeded
    degraded  -- running, but at least one dealership is currently failing to
                 collect (or no check has succeeded yet). A dealership added
                 in the last couple of hours that is still doing its first
                 pull does NOT count: that is onboarding, not a fault, and is
                 reported in ``businesses_onboarding`` instead.
    error     -- the database or the scheduler is down
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter
from sqlalchemy import text

from app.database.database import session_scope
from app.scheduler.scheduler import get_scheduler
from app.services import stats

router = APIRouter(tags=["health"])

# How long a newly added dealership may go without a successful check before it
# stops being "new" and starts being a problem. Generous on purpose: the first
# pull is the whole back catalogue, and a large listing on a slow night can take
# far longer than the 6 minutes Merit Auto Group needed for 840 reviews.
ONBOARDING_GRACE_MINUTES = 120


def _within_grace(created_at: str | None, minutes: int) -> bool:
    """Was this dealership added recently enough to still be settling in?

    Unparseable or missing timestamps return False, so anything we cannot date
    is treated as established and is allowed to report as degraded. Silence is
    the wrong default for a health check.
    """
    if not created_at:
        return False
    try:
        moment = dt.datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - moment
    return dt.timedelta(0) <= age <= dt.timedelta(minutes=minutes)


@router.get("/health")
def health():
    scheduler_status = get_scheduler().status()

    database_ok = True
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1")).scalar()
    except Exception:
        database_ok = False

    totals = stats.global_stats() if database_ok else {}
    businesses = stats.business_stats() if database_ok else []
    active = [b for b in businesses if b["active"]]

    failing = [b["name"] for b in active if b["status"] == "error"]

    # A dealership added minutes ago has no successful check yet because it is
    # still doing its FIRST pull, and a first pull fetches the whole back
    # catalogue: Merit Auto Group's took 6m08s for 840 reviews. Reporting that
    # as "degraded" made a perfectly healthy onboarding look like a fault, and
    # the dashboard sent somebody to Administration to correct configuration
    # that was already correct.
    #
    # Bounded by age as well as state, so a dealership genuinely stuck at
    # "pending" -- created but never picked up -- surfaces as degraded once the
    # grace window passes instead of hiding here for ever. A first pull that
    # FAILS lands in `failing` above and is unaffected by any of this.
    onboarding = [
        b["name"] for b in active
        if b["last_successful_check"] is None
        and not b["initial_sync_done"]
        and b["status"] in ("pending", "checking")
        and _within_grace(b.get("created_at"), ONBOARDING_GRACE_MINUTES)
    ]
    never_succeeded = [
        b["name"] for b in active
        if b["last_successful_check"] is None and b["name"] not in onboarding
    ]

    if not database_ok or not scheduler_status["running"]:
        status = "error"
    elif not active:
        status = "degraded"
    elif failing or never_succeeded:
        status = "degraded"
    else:
        status = "ok"

    detail = None
    if not database_ok:
        detail = "Database is unreachable."
    elif not scheduler_status["running"]:
        detail = "Scheduler is not running; no automatic checks will happen."
    elif not active:
        detail = "No dealership is configured. Set the PLACE_ID values in .env."
    elif failing:
        detail = f"Collection is currently failing for: {', '.join(failing)}."
    elif never_succeeded:
        detail = f"No successful check yet for: {', '.join(never_succeeded)}."
    elif onboarding:
        detail = (
            f"First collection in progress for: {', '.join(onboarding)}. "
            "This is normal for a newly added dealership."
        )

    last_ok = totals.get("last_successful_check") or {}
    last_any = totals.get("last_check") or {}

    return {
        "status": status,
        "detail": detail,
        "database": "ok" if database_ok else "error",
        "scheduler_running": scheduler_status["running"],
        "last_check": last_any.get("started_at"),
        "last_successful_check": last_ok.get("started_at"),
        "next_check": scheduler_status["next_check"],
        "check_interval_minutes": scheduler_status["interval_minutes"],
        "businesses_configured": len(active),
        "businesses_healthy": len(active) - len(set(failing) | set(never_succeeded)),
        # Counted separately: these are not unhealthy, they are new.
        "businesses_onboarding": len(onboarding),
        "total_reviews_collected": totals.get("total_reviews_collected", 0),
        "unprocessed_reviews": totals.get("unprocessed_reviews", 0),
    }
