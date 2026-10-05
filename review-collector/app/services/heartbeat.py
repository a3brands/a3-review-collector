"""A once-a-day "the collector is alive" email.

Every other alert here fires on an event: a new review, a failure, a recovery,
or 48 hours of total silence. Between those, a quiet day and a dead process look
exactly the same in an inbox -- nothing arrives either way. On a laptop that
sleeps, gets closed, and is off overnight, "no mail" is the normal state of a
broken system as well as a working one.

So this sends one message a day saying what was collected, from how many
dealerships, and what failed. Its value is that it arrives at all: a morning
with no heartbeat is a morning to go and look.

Catch-up, not a clock. The Mac is off overnight, so a job pinned to an hour is
a job that silently never runs -- exactly the trap that stopped the 03:15 backup
from ever firing. Instead the due check runs on a short interval and asks "is it
past the hour, and has today's already gone?", so a machine woken at 09:40 still
sends the 08:00 heartbeat.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy import func

from app.config import Settings, get_settings
from app.database.database import session_scope
from app.database.models import Business, CheckRun, Review
from app.services import notifier, stats

logger = logging.getLogger("heartbeat")

STATE_FILE = Path("data/heartbeat-state.json")


# ---------------------------------------------------------------------------
# The "have we already sent one today" marker
# ---------------------------------------------------------------------------
def _read_state() -> Dict:
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:
        # No marker yet, or an unreadable one. Losing it costs one duplicate
        # heartbeat, which is far better than crashing the scheduler.
        return {}


def _write_state(state: Dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), "utf-8")
        tmp.replace(STATE_FILE)
    except Exception as exc:  # pragma: no cover - marker loss is survivable
        logger.error("Could not write the heartbeat marker: %s", exc)


def last_sent_day() -> Optional[str]:
    value = _read_state().get("last_sent_day")
    return value if isinstance(value, str) else None


def mark_sent(day: str) -> None:
    _write_state({"last_sent_day": day})


def is_due(now: Optional[dt.datetime] = None, settings: Optional[Settings] = None) -> bool:
    """True when today's heartbeat is past its hour and has not been sent."""
    settings = settings or get_settings()
    if not settings.notify_on_heartbeat:
        return False
    now = now or dt.datetime.now()
    if now.hour < settings.heartbeat_hour:
        return False
    return last_sent_day() != now.date().isoformat()


# ---------------------------------------------------------------------------
# What the day actually looked like
# ---------------------------------------------------------------------------
def summarise(now: Optional[dt.datetime] = None) -> Dict:
    """Per-dealership counts for the last 24 hours, plus the run outcome.

    Deliberately a rolling 24 hours rather than "since midnight": the machine is
    often asleep at midnight, and a window that starts when nobody was watching
    would report a morning as empty.
    """
    now = now or dt.datetime.utcnow()
    since = now - dt.timedelta(hours=24)

    results = []
    with session_scope() as session:
        businesses = session.query(Business).filter_by(active=True).order_by(Business.name).all()
        for b in businesses:
            found = (
                session.query(func.count(Review.id))
                .filter(Review.business_id == b.id, Review.detected_at >= since)
                .scalar()
            ) or 0
            runs = (
                session.query(func.count(CheckRun.id))
                .filter(CheckRun.business_id == b.id, CheckRun.started_at >= since)
                .scalar()
            ) or 0
            failed = (
                session.query(func.count(CheckRun.id))
                .filter(
                    CheckRun.business_id == b.id,
                    CheckRun.started_at >= since,
                    CheckRun.status == "failed",
                )
                .scalar()
            ) or 0
            results.append({
                "business_name": b.name,
                # 'failed' here means every check in the window failed. One
                # failure among twenty successes is a retry, not an outage, and
                # reporting it as one would make the heartbeat cry wolf daily.
                "status": "failed" if runs and failed == runs else "success",
                "reviews_found": found,
                "new_reviews": found,
                "skipped_existing": 0,
                "checks": runs,
                "failed_checks": failed,
                "error_type": "AllChecksFailed" if runs and failed == runs else None,
                "error_message": f"{failed} of {runs} checks failed in 24h" if runs and failed == runs else None,
            })
    return {"results": results, "window_hours": 24}


def link_problems() -> List[Dict]:
    """Dealerships whose review links would open Google without their name.

    Either no listing id is known for them, or some stored links still carry
    `0x0:0x0`. Every check repairs the second on its own, so anything listed
    here survived a repair and needs a person to look.
    """
    problems = []
    with session_scope() as session:
        for b in session.query(Business).filter_by(active=True).order_by(Business.name):
            bare = (
                session.query(func.count(Review.id))
                .filter(Review.business_id == b.id, Review.review_url.contains("0x0:0x0"))
                .scalar()
            ) or 0
            if bare or not b.feature_id:
                problems.append({"business_name": b.name, "bare_links": bare,
                                 "missing_listing_id": not b.feature_id})
    return problems


def send(trigger: str = "scheduled", now: Optional[dt.datetime] = None,
         settings: Optional[Settings] = None) -> Dict:
    """Send today's heartbeat and record that it went."""
    settings = settings or get_settings()
    now = now or dt.datetime.now()

    summary = summarise()
    totals = stats.global_stats()
    totals["link_problems"] = link_problems()
    outcome = notifier.send_event(
        "heartbeat",
        results=summary["results"],
        totals=totals,
        reviews=stats.pending_reviews(5),
        settings=settings,
    )
    # Marked regardless of the send result. A mail server that is refusing
    # connections would otherwise have this retry every interval all day.
    mark_sent(now.date().isoformat())
    logger.info(
        "Heartbeat (%s): %d dealership(s), %d new in 24h",
        trigger,
        len(summary["results"]),
        sum(r["new_reviews"] for r in summary["results"]),
    )
    return outcome


def run_if_due(settings: Optional[Settings] = None) -> Optional[Dict]:
    """The scheduler's entry point. Cheap and silent on all but one call a day."""
    settings = settings or get_settings()
    now = dt.datetime.now()
    if not is_due(now, settings):
        return None
    return send("scheduled", now=now, settings=settings)
