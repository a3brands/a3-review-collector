"""Read-only aggregates for the dashboard and /health."""
from __future__ import annotations

from typing import Dict, List, Optional

from sqlalchemy import func

from app.database.database import session_scope
from app.database.models import Business, CheckRun, Review, iso_utc


def _last_run(session, business_id: int, status: Optional[str] = None) -> Optional[CheckRun]:
    query = session.query(CheckRun).filter(CheckRun.business_id == business_id)
    if status:
        query = query.filter(CheckRun.status == status)
    return query.order_by(CheckRun.started_at.desc()).first()


def business_stats() -> List[Dict]:
    out: List[Dict] = []
    with session_scope() as session:
        for business in session.query(Business).order_by(Business.id).all():
            total = session.query(func.count(Review.id)).filter(
                Review.business_id == business.id
            ).scalar() or 0
            unprocessed = session.query(func.count(Review.id)).filter(
                Review.business_id == business.id, Review.processed.is_(False)
            ).scalar() or 0

            last = _last_run(session, business.id)
            last_ok = _last_run(session, business.id, "success")
            last_fail = _last_run(session, business.id, "failed")

            if not business.active:
                status = "not_configured"
            elif last is None:
                status = "pending"
            elif last.status == "success":
                status = "online"
            elif last.status == "running":
                status = "checking"
            else:
                status = "error"

            out.append(
                {
                    "id": business.id,
                    "key": business.key,
                    "name": business.name,
                    "place_id": business.place_id,
                    "google_url": business.google_url,
                    "gbp_location_name": business.gbp_location_name,
                    "active": business.active,
                    "initial_sync_done": business.initial_sync_done,
                    # Needed to tell a dealership that is still doing its first
                    # pull from one that has been stuck pending for days.
                    "created_at": iso_utc(business.created_at),
                    "status": status,
                    "total_reviews": total,
                    "unprocessed_reviews": unprocessed,
                    "processed_reviews": total - unprocessed,
                    "last_check": last.to_dict() if last else None,
                    "last_successful_check": last_ok.to_dict() if last_ok else None,
                    "last_error": last_fail.to_dict() if last_fail else None,
                    "new_reviews_last_check": last.new_reviews if last else 0,
                }
            )
    return out


def global_stats() -> Dict:
    with session_scope() as session:
        total = session.query(func.count(Review.id)).scalar() or 0
        unprocessed = session.query(func.count(Review.id)).filter(
            Review.processed.is_(False)
        ).scalar() or 0
        last_ok = (
            session.query(CheckRun)
            .filter(CheckRun.status == "success")
            .order_by(CheckRun.started_at.desc())
            .first()
        )
        last_any = session.query(CheckRun).order_by(CheckRun.started_at.desc()).first()
        last_fail = (
            session.query(CheckRun)
            .filter(CheckRun.status == "failed")
            .order_by(CheckRun.started_at.desc())
            .first()
        )
        return {
            "total_reviews_collected": total,
            "unprocessed_reviews": unprocessed,
            "processed_reviews": total - unprocessed,
            "last_check": last_any.to_dict() if last_any else None,
            "last_successful_check": last_ok.to_dict() if last_ok else None,
            "last_error": last_fail.to_dict() if last_fail else None,
        }


def pending_reviews(limit: int = 20) -> List[Dict]:
    """Reviews still waiting for A3 -- used in notification emails."""
    from sqlalchemy.orm import joinedload

    with session_scope() as session:
        rows = (
            session.query(Review)
            .options(joinedload(Review.business))
            .filter(Review.processed.is_(False))
            .order_by(Review.detected_at.desc())
            .limit(limit)
            .all()
        )
        return [row.to_a3_payload() for row in rows]


def unnotified_reviews(limit: int = 25, max_age_hours: int = 24) -> List[Dict]:
    """Reviews detected recently that have never appeared in an email.

    This is what a Refresh notification contains. It is deliberately NOT "the
    unprocessed queue": a review that has waited a year has already been
    emailed once, and re-sending it every time somebody clicks Refresh is spam.
    """
    import datetime as _dt

    from sqlalchemy.orm import joinedload

    from app.database.models import utcnow as _now

    cutoff = _now() - _dt.timedelta(hours=max_age_hours)
    with session_scope() as session:
        rows = (
            session.query(Review)
            .options(joinedload(Review.business))
            .filter(Review.notified_at.is_(None))
            .filter(Review.detected_at >= cutoff)
            .order_by(Review.rating.asc().nullslast(), Review.detected_at.asc())
            .limit(limit)
            .all()
        )
        return [row.to_a3_payload() for row in rows]


def mark_notified(review_ids: List[str]) -> int:
    """Record that these reviews have now been emailed. Idempotent."""
    from app.database.models import utcnow as _now

    if not review_ids:
        return 0
    stamped = _now()
    with session_scope() as session:
        rows = (
            session.query(Review)
            .filter(Review.review_id.in_(review_ids))
            .filter(Review.notified_at.is_(None))
            .all()
        )
        for row in rows:
            row.notified_at = stamped
        return len(rows)


def unsynced_reviews(limit: int = 100) -> List[Dict]:
    """Reviews A3 has never accepted. Ordered oldest-first so history fills in."""
    from sqlalchemy.orm import joinedload

    with session_scope() as session:
        rows = (
            session.query(Review)
            .options(joinedload(Review.business))
            .filter(Review.a3_synced_at.is_(None))
            .order_by(Review.detected_at.asc(), Review.id.asc())
            .limit(limit)
            .all()
        )
        return [row.to_a3_payload() for row in rows]


def mark_a3_synced(review_ids: List[str]) -> int:
    from app.database.models import utcnow as _now

    if not review_ids:
        return 0
    stamped = _now()
    with session_scope() as session:
        rows = (
            session.query(Review)
            .filter(Review.review_id.in_(review_ids))
            .filter(Review.a3_synced_at.is_(None))
            .all()
        )
        for row in rows:
            row.a3_synced_at = stamped
        return len(rows)


def queue_summary() -> Dict:
    """What the queue actually needs from a human, not just how big it is.

    The dashboard leads with this: how many are waiting, how many are angry,
    and how long the oldest one has been sitting there. A count alone does not
    tell you whether anything is going wrong.
    """
    from sqlalchemy import func as _func

    with session_scope() as session:
        base = session.query(Review).filter(Review.processed.is_(False))
        waiting = base.count()
        negative = base.filter(Review.rating <= 2).count()
        oldest = (
            base.order_by(Review.detected_at.asc())
            .with_entities(Review.detected_at)
            .first()
        )
        worst = base.with_entities(_func.min(Review.rating)).scalar()

        oldest_posted = (
            base.filter(Review.review_date.isnot(None))
            .order_by(Review.review_date.asc())
            .with_entities(Review.review_date)
            .first()
        )
        oldest_at = oldest[0] if oldest else None
        oldest_age_days = None
        if oldest_at is not None:
            from app.database.models import utcnow

            oldest_age_days = max(0, (utcnow() - oldest_at).days)

        # Age since the customer POSTED is what matters to the dealership -- a
        # review left unanswered for four months is a problem regardless of
        # when this collector happened to notice it.
        oldest_posted_at = oldest_posted[0] if oldest_posted else None
        oldest_posted_days = None
        if oldest_posted_at is not None:
            from app.database.models import utcnow as _now

            oldest_posted_days = max(0, (_now() - oldest_posted_at).days)

        return {
            "waiting": waiting,
            "negative": negative,
            "worst_rating": worst,
            "oldest_detected_at": iso_utc(oldest_at),
            "oldest_age_days": oldest_age_days,
            "oldest_posted_at": iso_utc(oldest_posted_at),
            "oldest_posted_days": oldest_posted_days,
        }


def recent_checks(limit: int = 20) -> List[Dict]:
    with session_scope() as session:
        rows = (
            session.query(CheckRun).order_by(CheckRun.started_at.desc()).limit(limit).all()
        )
        return [row.to_dict() for row in rows]
