"""The contract with the A3 Review Responder.

A3 only ever needs two calls:
    GET  /api/reviews/new                     -> reviews waiting to be answered
    POST /api/reviews/{review_id}/processed   -> acknowledge one

Everything else here is convenience for debugging.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import joinedload

from app.api.deps import require_api_key
from app.database.database import session_scope
from app.database.models import Business, Review, utcnow

logger = logging.getLogger("api")

router = APIRouter(prefix="/api/reviews", tags=["reviews"])


@router.get("/new")
def get_new_reviews(
    limit: int = Query(50, ge=1, le=500, description="Maximum reviews to return"),
    sort: str = Query(
        "arrival",
        pattern="^(arrival|oldest|newest|urgent)$",
        description="arrival = order detected (default; what A3 should use). "
                    "oldest/newest = by the date the customer posted. "
                    "urgent = lowest rating first, then longest waiting.",
    ),
    business: Optional[str] = Query(
        None, description="Optional business key filter: 'bmw_fwb' or 'mb_fwb'"
    ),
    min_rating: Optional[int] = Query(None, ge=1, le=5),
    max_rating: Optional[int] = Query(None, ge=1, le=5),
    _: str = Depends(require_api_key),
):
    """Unprocessed reviews, oldest first so A3 answers in the order they arrived."""
    with session_scope() as session:
        query = (
            session.query(Review)
            .options(joinedload(Review.business))
            .filter(Review.processed.is_(False))
        )
        if business:
            query = query.join(Business).filter(Business.key == business)
        if min_rating is not None:
            query = query.filter(Review.rating >= min_rating)
        if max_rating is not None:
            query = query.filter(Review.rating <= max_rating)

        # Ordering is applied in SQL, not in the browser: sorting only the
        # rows that happen to be on screen would silently hide the angriest
        # review whenever the queue is longer than one page.
        if sort == "urgent":
            query = query.order_by(
                Review.rating.asc().nullslast(),
                Review.review_date.asc().nullslast(),
                Review.id.asc(),
            )
        elif sort == "newest":
            query = query.order_by(Review.review_date.desc().nullslast(), Review.id.desc())
        elif sort == "oldest":
            query = query.order_by(Review.review_date.asc().nullslast(), Review.id.asc())
        else:  # arrival -- unchanged default, so A3 keeps getting them in the order found
            query = query.order_by(Review.detected_at.asc(), Review.id.asc())

        # `total` is the whole queue; `count` is just this page. Without it the
        # dashboard heading claimed "18 waiting" was "10".
        total = query.order_by(None).count()
        rows = query.limit(limit).all()
        payload = [row.to_a3_payload() for row in rows]

    logger.info("A3 fetched %d unprocessed review(s)", len(payload))
    return {"count": len(payload), "total": total, "reviews": payload}


@router.post("/{review_id}/processed", status_code=status.HTTP_200_OK)
def mark_processed(review_id: str, _: str = Depends(require_api_key)):
    """A3 calls this after it has generated and handled a response.

    Idempotent: calling it twice is fine and never raises.
    """
    with session_scope() as session:
        row = (
            session.query(Review)
            .options(joinedload(Review.business))
            .filter(Review.review_id == review_id)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No review with review_id {review_id!r}.",
            )
        already = row.processed
        if not already:
            row.processed = True
            row.processed_at = utcnow()
        payload = row.to_a3_payload()

    if already:
        logger.info("Review %s was already marked processed", review_id)
    else:
        logger.info("Review %s marked processed by A3", review_id)

    return {"ok": True, "already_processed": already, "review": payload}


@router.post("/{review_id}/unprocessed", status_code=status.HTTP_200_OK)
def mark_unprocessed(review_id: str, _: str = Depends(require_api_key)):
    """Undo -- put a review back in the queue (useful if A3 errored downstream)."""
    with session_scope() as session:
        row = session.query(Review).filter(Review.review_id == review_id).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail=f"No review with review_id {review_id!r}.")
        row.processed = False
        row.processed_at = None
    logger.info("Review %s returned to the unprocessed queue", review_id)
    return {"ok": True, "review_id": review_id, "processed": False}


@router.get("")
def list_reviews(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sort: str = Query("newest", pattern="^(oldest|newest|urgent)$"),
    processed: Optional[bool] = Query(None),
    business: Optional[str] = Query(None),
    _: str = Depends(require_api_key),
):
    """All reviews, newest first. For debugging and the dashboard."""
    with session_scope() as session:
        query = session.query(Review).options(joinedload(Review.business))
        if processed is not None:
            query = query.filter(Review.processed.is_(processed))
        if business:
            query = query.join(Business).filter(Business.key == business)
        total = query.count()
        if sort == "urgent":
            query = query.order_by(
                Review.rating.asc().nullslast(),
                Review.review_date.asc().nullslast(),
                Review.id.asc(),
            )
        elif sort == "oldest":
            query = query.order_by(Review.review_date.asc().nullslast(), Review.id.asc())
        else:
            query = query.order_by(Review.review_date.desc().nullslast(), Review.id.desc())

        rows = query.offset(offset).limit(limit).all()
        payload = [row.to_a3_payload() for row in rows]
    return {"total": total, "count": len(payload), "offset": offset, "reviews": payload}


@router.get("/{review_id}")
def get_review(review_id: str, _: str = Depends(require_api_key)):
    with session_scope() as session:
        row = (
            session.query(Review)
            .options(joinedload(Review.business))
            .filter(Review.review_id == review_id)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"No review with review_id {review_id!r}.")
        return row.to_a3_payload()
