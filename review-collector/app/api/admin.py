"""Dashboard + operations endpoints (same API key as A3)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.api.deps import require_api_key
from app.assets import asset_version
from app.collector.registry import get_backend_status
from app.config import get_settings
from app.scheduler.scheduler import get_scheduler
from app.services import notifier, stats

logger = logging.getLogger("api")

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/status")
def status(_: str = Depends(require_api_key)):
    """Everything the dashboard renders, in one call."""
    settings = get_settings()
    scheduler = get_scheduler()
    return {
        "businesses": stats.business_stats(),
        "totals": stats.global_stats(),
        "queue": stats.queue_summary(),
        "scheduler": scheduler.status(),
        "backends": get_backend_status(settings),
        "asset_version": asset_version(),
        "config": {
            "check_interval_minutes": settings.check_interval_minutes,
            "collector_backend": settings.collector_backend,
            "reviews_per_check": settings.reviews_per_check,
            "initial_sync": settings.initial_sync,
            "initial_sync_mark_processed": settings.initial_sync_mark_processed,
        },
    }


@router.get("/checks")
def checks(limit: int = Query(20, ge=1, le=200), _: str = Depends(require_api_key)):
    return {"checks": stats.recent_checks(limit)}


@router.post("/check-now")
def check_now(request: Request, _: str = Depends(require_api_key)):
    """The dashboard's [ Check Now ] button. Checks both dealerships.

    Emails the outcome when NOTIFY_ON_CHECK_NOW is on. Sending happens on a
    worker thread, so a slow or broken mail server cannot delay this response
    or hide the check result.
    """
    result = get_scheduler().trigger_now()

    outcome = notifier.send_event(
        "check_now",
        actor=_actor(request),
        results=result.get("results") or [],
        totals=stats.global_stats(),
        reviews=stats.pending_reviews(20),
    )
    result["notification"] = outcome
    return result


class NotifyRequest(BaseModel):
    event: str = "refresh"


@router.post("/notify")
def notify(payload: NotifyRequest, request: Request, _: str = Depends(require_api_key)):
    """Fired by the dashboard when someone clicks Refresh by hand.

    Deliberately NOT called by the dashboard's 30-second auto-refresh: that
    would be roughly 2,880 messages a day per open tab, which is precisely the
    volume that gets a sender moved to Spam.
    """
    if payload.event not in ("refresh", "check_now"):
        return {"sent": False, "reason": f"unknown event {payload.event!r}"}

    settings = get_settings()
    fresh = stats.unnotified_reviews(limit=25, max_age_hours=settings.notify_max_age_hours)

    if not fresh:
        # Nothing new since the last email. Sending "here are the same 18
        # reviews again" is exactly the noise this is meant to avoid.
        logger.info("Refresh: no newly detected reviews to report; no email sent")
        return {
            "sent": False,
            "reason": "no newly detected reviews since the last notification",
            "new_reviews": 0,
        }

    ids = [r["review_id"] for r in fresh]
    outcome = notifier.send_event(
        payload.event,
        actor=_actor(request),
        results=[],
        totals=stats.global_stats(),
        reviews=fresh,
        on_delivered=lambda: stats.mark_notified(ids),
    )
    outcome["new_reviews"] = len(fresh)
    return outcome


def _actor(request: Request) -> str:
    client = request.client.host if request.client else "unknown"
    agent = (request.headers.get("user-agent") or "")[:60]
    return f"{client} ({agent})" if agent else client


@router.post("/scheduler/start")
def scheduler_start(_: str = Depends(require_api_key)):
    return get_scheduler().start()


@router.post("/scheduler/stop")
def scheduler_stop(_: str = Depends(require_api_key)):
    return get_scheduler().stop()


@router.post("/scheduler/restart")
def scheduler_restart(_: str = Depends(require_api_key)):
    return get_scheduler().restart()
