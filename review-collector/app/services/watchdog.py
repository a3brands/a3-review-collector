"""The alarm for the failure that does not look like one.

Every other failure in this system announces itself. A dealership that cannot be
reached raises AccessBlocked, a listing that does not match raises an identity
error, a broken parse says the markup changed. Those all show up as a failed
check and a failure email.

The dangerous one is quieter. Google serves a listing with the reviews missing,
or throttles a scraper it has decided it does not like. The page loads, the
headline figures read fine, nothing raises, the check records success with zero
found, and the dashboard shows a perfectly healthy system that has silently
stopped collecting. Nobody finds out until a customer asks why their reviews
stopped arriving.

The only way to catch that is to notice the absence. Across the whole roster,
reviews arrive constantly: BMW of Fort Walton Beach alone averages two a day.
A stretch of days with nothing from anyone is not a quiet week, it is a broken
system.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Dict, Optional

from sqlalchemy import func

from app.config import Settings, get_settings
from app.database.models import Business, CheckRun, Review, utcnow

logger = logging.getLogger("watchdog")


def assess(session, settings: Optional[Settings] = None,
           now: Optional[dt.datetime] = None) -> Dict:
    """Is the system still actually collecting?

    Returns the verdict and the numbers behind it, because an alert that cannot
    say why it fired gets muted rather than acted on.
    """
    settings = settings or get_settings()
    now = now or utcnow()

    active = session.query(func.count(Business.id)).filter(Business.active.is_(True)).scalar() or 0
    if active == 0:
        return {"stalled": False, "reason": "no active dealerships to collect from"}

    newest = session.query(func.max(Review.detected_at)).scalar()
    if newest is None:
        # Nothing has ever been collected. That is a setup state, not a stall,
        # and alerting on it would fire on every fresh install.
        return {"stalled": False, "reason": "nothing collected yet"}

    quiet_hours = (now - newest).total_seconds() / 3600.0

    # A stall is only meaningful if the system has been trying. If nothing has
    # been checked recently either, the collector is down, and that is a
    # different alarm with a different fix.
    recent_checks = (
        session.query(func.count(CheckRun.id))
        .filter(CheckRun.started_at >= now - dt.timedelta(hours=settings.stall_alert_hours))
        .scalar()
    ) or 0

    successes = (
        session.query(func.count(CheckRun.id))
        .filter(
            CheckRun.started_at >= now - dt.timedelta(hours=settings.stall_alert_hours),
            CheckRun.status == "success",
        )
        .scalar()
    ) or 0

    stalled = (
        quiet_hours >= settings.stall_alert_hours
        # Checks have been running and reporting success the whole time. That
        # combination, no reviews and no complaints, is the signature.
        and successes > 0
    )

    return {
        "stalled": stalled,
        "quiet_hours": round(quiet_hours, 1),
        "threshold_hours": settings.stall_alert_hours,
        "active_businesses": active,
        "checks_in_window": recent_checks,
        "successful_checks_in_window": successes,
        "newest_review_at": newest,
        "reason": (
            f"no review has arrived from any of {active} dealership(s) in "
            f"{quiet_hours:.0f} hours, while {successes} check(s) reported success"
            if stalled
            else f"last review {quiet_hours:.0f}h ago, under the {settings.stall_alert_hours}h threshold"
        ),
    }


def check_and_alert(notifier, session, settings: Optional[Settings] = None) -> Dict:
    """Assess, and raise the alarm once if the system has gone quiet.

    Rate limiting lives in the notifier, so a stall that lasts a week produces a
    reminder rather than a check-by-check flood.
    """
    settings = settings or get_settings()
    verdict = assess(session, settings)

    if not verdict["stalled"]:
        logger.debug("watchdog: %s", verdict["reason"])
        return verdict

    if not settings.stall_alert_enabled:
        logger.warning("watchdog: %s (alerting disabled)", verdict["reason"])
        return verdict

    logger.error("watchdog: %s", verdict["reason"])
    try:
        notifier.send_event("stalled", totals=verdict, settings=settings)
    except Exception:  # pragma: no cover - alerting must never break a cycle
        logger.exception("watchdog: could not send the stall alert")
    return verdict
