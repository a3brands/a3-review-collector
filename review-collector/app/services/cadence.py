"""How often each dealership actually needs checking.

Every dealership used to be checked on the same 15 minute clock. Measured over
770 checks, 96% of them found nothing: BMW of Fort Walton Beach returned
something 4.7% of the time and Mercedes-Benz 3.1%. Mercedes receives a review
about once every two days and was being checked 192 times in between, each check
costing roughly 30 seconds of a real browser.

That waste is the capacity. A dealership is now checked at a pace set by how
often it actually receives reviews, which leaves the busy ones on the fast clock
and frees almost all of the budget for more dealerships.

Nothing here is stored: the pace is derived from the reviews already on file and
the last check, so it adjusts itself as a dealership gets busier or quieter and
there is no extra state to fall out of step with reality.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
from typing import Optional, Tuple

from sqlalchemy import func

from app.config import Settings, get_settings
from app.database.models import Business, CheckRun, Review, utcnow

logger = logging.getLogger("cadence")

FAST, MEDIUM, SLOW, DORMANT = "fast", "medium", "slow", "dormant"


def review_velocity(session, business_id: int, days: int = 30) -> float:
    """Reviews per day for this dealership over the recent window.

    Counted from review_date rather than when we detected them, so a backfill of
    old reviews cannot make a quiet dealership look busy.
    """
    cutoff = utcnow() - dt.timedelta(days=days)
    count = (
        session.query(func.count(Review.id))
        .filter(Review.business_id == business_id, Review.review_date >= cutoff)
        .scalar()
    ) or 0
    return count / float(days)


def tier_for(velocity: float, settings: Settings) -> Tuple[str, int]:
    """(tier name, interval in minutes) for a given reviews-per-day rate."""
    if velocity >= settings.adaptive_fast_per_day:
        return FAST, settings.adaptive_fast_minutes
    if velocity >= settings.adaptive_medium_per_day:
        return MEDIUM, settings.adaptive_medium_minutes
    if velocity >= settings.adaptive_dormant_per_day:
        return SLOW, settings.adaptive_slow_minutes
    return DORMANT, settings.adaptive_dormant_minutes


def stagger_offset(key: str, interval_minutes: int, tick_minutes: int) -> int:
    """A fixed position for this dealership inside its own interval.

    Dealerships onboarded on the same day would otherwise share a due time
    forever, so the load arrives as a spike every few hours and the spike, not
    the daily average, is what caps how many can be served. The offset is
    derived from the key rather than stored or randomised: it never moves, and
    it does not need a migration or a column.

    Measured in whole scheduler ticks, because a due time between ticks cannot
    be acted on anyway.
    """
    slots = max(1, interval_minutes // max(1, tick_minutes))
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return (int.from_bytes(digest[:4], "big") % slots) * tick_minutes


def _slot(moment: dt.datetime, interval_minutes: int, offset_minutes: int) -> int:
    """Which interval-sized window a moment falls in, shifted by the offset."""
    minutes = moment.replace(tzinfo=dt.timezone.utc).timestamp() / 60.0
    return int((minutes - offset_minutes) // max(1, interval_minutes))


def _last_check(session, business_id: int, *, full_only: bool = False) -> Optional[CheckRun]:
    query = (
        session.query(CheckRun)
        .filter(CheckRun.business_id == business_id, CheckRun.status == "success")
    )
    if full_only:
        # A fast-path check reads the headline count and stops. It proves nothing
        # about reviews that were edited or removed without changing the count,
        # so it does not reset the full-scan clock.
        query = query.filter((CheckRun.fast_path.is_(False)) | (CheckRun.fast_path.is_(None)))
    return query.order_by(CheckRun.started_at.desc()).first()


def plan_for(session, business: Business, settings: Optional[Settings] = None,
             now: Optional[dt.datetime] = None) -> dict:
    """Whether this dealership is due, and how it should be checked.

    Returns the decision plus the reasoning, because "why was this one skipped"
    is the first question anyone asks of a scheduler that skips things.
    """
    settings = settings or get_settings()
    now = now or utcnow()

    if not settings.adaptive_interval_enabled:
        return {"due": True, "tier": FAST, "interval_minutes": settings.check_interval_minutes,
                "velocity": None, "full_scan": True, "reason": "adaptive pacing is off"}

    # A dealership nobody has ever collected from is always due: there is no
    # velocity to measure and no history to protect.
    if not business.initial_sync_done:
        return {"due": True, "tier": FAST, "interval_minutes": settings.adaptive_fast_minutes,
                "velocity": None, "full_scan": True, "reason": "first collection"}

    velocity = review_velocity(session, business.id, settings.adaptive_window_days)
    tier, interval = tier_for(velocity, settings)

    last = _last_check(session, business.id)
    if last is None or last.started_at is None:
        return {"due": True, "tier": tier, "interval_minutes": interval, "velocity": velocity,
                "full_scan": True, "reason": "no successful check on record"}

    waited = (now - last.started_at).total_seconds() / 60.0

    if settings.adaptive_stagger_enabled:
        # Due when the clock has crossed into a new window for THIS dealership.
        # Two dealerships on the same interval with different offsets cross on
        # different ticks, which is what spreads the load.
        offset = stagger_offset(business.key, interval, settings.check_interval_minutes)
        crossed = _slot(now, interval, offset) != _slot(last.started_at, interval, offset)
        # A window boundary landing just after a check would otherwise mean two
        # checks minutes apart. Half an interval must have passed as well, which
        # costs nothing in steady state because checks settle near the start of
        # their window anyway.
        due = crossed and waited >= interval * 0.5
    else:
        offset = 0
        due = waited >= interval

    # Even a dealership on the slow clock gets a full read periodically, so an
    # edited or deleted review is eventually noticed rather than hidden behind an
    # unchanged headline count forever.
    last_full = _last_check(session, business.id, full_only=True)
    full_scan = (
        last_full is None
        or last_full.started_at is None
        or (now - last_full.started_at).total_seconds() / 3600.0 >= settings.full_scan_every_hours
    )
    # A recent review still shown as unanswered: read the reviews on every
    # check until the dealership's reply is seen. The shortcut only compares
    # the headline count, which a reply does not change, so the dealership
    # answering would go unnoticed for up to a day, and an approval email could
    # go out for a review already answered (two BMW reviews on 2026-10-06).
    if not full_scan:
        recent_unanswered = (
            session.query(Review.id)
            .filter(
                Review.business_id == business.id,
                Review.review_id_is_native.is_(True),
                Review.owner_replied.is_(False),
                Review.detected_at >= now - dt.timedelta(days=7),
            )
            .first()
        )
        full_scan = recent_unanswered is not None

    return {
        "due": due,
        "tier": tier,
        "interval_minutes": interval,
        "velocity": round(velocity, 3),
        "full_scan": full_scan,
        "waited_minutes": round(waited, 1),
        "offset_minutes": offset,
        "reason": (
            f"{velocity:.2f} reviews/day puts it on the {tier} clock ({interval} min); "
            + (f"waited {waited:.0f} min" if due else f"only {waited:.0f} min since the last check")
        ),
    }
