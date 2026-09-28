"""SQLAlchemy models.

Everything lives in one SQLite file so the whole system has zero recurring
cost and survives restarts with no external service.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    Boolean,
    Float,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    """Naive UTC. Stored naive so SQLite comparisons stay simple."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def iso_utc(value: Optional[dt.datetime]) -> Optional[str]:
    """Serialise as unambiguous UTC.

    Every timestamp this service emits ends in 'Z' so A3 and the dashboard never
    have to guess a timezone.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z"


class Base(DeclarativeBase):
    pass


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    google_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # What the Responder last sent as google_url, kept apart from google_url
    # itself. google_url may hold the listing the collector resolved from a
    # search link; this is how the registry sync tells "the admin changed the
    # link" (replace it) from "the same search link again" (keep the listing).
    configured_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    place_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    gbp_location_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    initial_sync_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # The listing's own headline figures, as Google publishes them. Stored rather
    # than recomputed: an average over the reviews we hold skews high, because we
    # only read the most recent few hundred.
    overall_rating: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lifetime_reviews: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Google Maps identity of the listing. Needed to open a review ON the
    # dealership's profile rather than as a bare, unattributed review page.
    feature_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    reviews = relationship("Review", back_populates="business", cascade="all, delete-orphan")
    checks = relationship("CheckRun", back_populates="business", cascade="all, delete-orphan")

    @property
    def profile_url(self) -> Optional[str]:
        """The public Google Business Profile for this listing.

        google_url is whatever was configured for the listing, which is the
        exact page when one was supplied. A place_id resolves to the same
        profile through Google's own redirect, so it is the fallback rather
        than a second-best link.
        """
        if self.google_url:
            return self.google_url
        if self.place_id:
            return f"https://www.google.com/maps/place/?q=place_id:{self.place_id}"
        return None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Business {self.key} {self.name!r}>"


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        # Hard duplicate protection. review_id is globally unique: GBP review
        # IDs already are, and locally generated fingerprints are salted with
        # the business key (see collector/deduplication.py).
        UniqueConstraint("review_id", name="uq_reviews_review_id"),
        Index("ix_reviews_business_processed", "business_id", "processed"),
        Index("ix_reviews_processed_detected", "processed", "detected_at"),
        Index("ix_reviews_review_date", "review_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )

    review_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # True when review_id came from Google, False when we had to fingerprint it.
    review_id_is_native: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    reviewer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    reviewer_profile_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rating: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    review_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    review_date: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    # Google's public UI only exposes relative dates ("2 weeks ago") on some
    # surfaces. When we had to derive review_date from one of those, we say so
    # rather than pretending we know the exact timestamp.
    review_date_is_approximate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    review_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Whether the dealership has already answered this review on Google.
    # A3 uses this to decide 'posted' vs 'pending_draft', so it must be
    # collected -- but it is deliberately never shown in notification emails,
    # where only the customer's own comment belongs.
    owner_replied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    owner_reply: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    owner_reply_date: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    source: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    detected_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    # When this review was included in an email. Separate from `processed`,
    # which means A3 has handled it. A review must be emailed exactly once,
    # regardless of how long it then sits in the queue.
    notified_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    # When this review was accepted by the A3 Review Responder. Separate again
    # from processed/notified: A3 ingesting a review says nothing about whether
    # anyone has replied to it.
    a3_synced_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    processed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    business = relationship("Business", back_populates="reviews")

    @property
    def google_review_id(self) -> str:
        """Google's own review id, without our business-key namespace.

        Our review_id is '<business_key>:<google id>' so two dealerships can
        never collide. A3 dedupes on Google's raw id (and has a second ingest
        path that writes it), so the prefix MUST be stripped before syncing or
        the same review becomes two rows there.
        """
        if self.review_id_is_native and ":" in self.review_id:
            return self.review_id.split(":", 1)[1]
        return self.review_id

    def to_a3_payload(self) -> dict:
        """The exact shape handed to the A3 Review Responder."""
        return {
            "business_name": self.business.name if self.business else None,
            "business_id": self.business_id,
            "business_key": self.business.key if self.business else None,
            "place_id": self.business.place_id if self.business else None,
            # The dealership's own Google profile, so anything rendering this
            # review can link to the listing it came from.
            "business_profile_url": self.business.profile_url if self.business else None,
            "review_id": self.review_id,
            "google_review_id": self.google_review_id,
            "owner_replied": self.owner_replied,
            "owner_reply": self.owner_reply,
            "owner_reply_date": iso_utc(self.owner_reply_date),
            "reviewer_name": self.reviewer_name,
            "reviewer_profile_url": self.reviewer_profile_url,
            "rating": self.rating,
            "star_rating": self.rating,
            "review_text": self.review_text,
            "review_date": iso_utc(self.review_date),
            "review_date_is_approximate": self.review_date_is_approximate,
            "review_url": self.review_url,
            "source": self.source,
            "detected_at": iso_utc(self.detected_at),
            "notified_at": iso_utc(self.notified_at),
            "a3_synced_at": iso_utc(self.a3_synced_at),
            "processed": self.processed,
            "processed_at": iso_utc(self.processed_at),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Review {self.review_id} rating={self.rating}>"


class CheckRun(Base):
    """One collection attempt for one business. Drives the dashboard + /health."""

    __tablename__ = "check_runs"
    __table_args__ = (Index("ix_check_runs_business_started", "business_id", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    backend: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    trigger: Mapped[str] = mapped_column(String(32), default="scheduled", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)  # running|success|failed
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reviews_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_reviews: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_existing: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    will_retry: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # True when the check read the listing's headline count, saw it unchanged and
    # stopped before opening the reviews. Recorded because a fast check proves
    # less than a full one, so it must not reset the full-scan clock.
    fast_path: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    business = relationship("Business", back_populates="checks")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "business_id": self.business_id,
            "business_name": self.business.name if self.business else None,
            "backend": self.backend,
            "trigger": self.trigger,
            "status": self.status,
            "started_at": iso_utc(self.started_at),
            "finished_at": iso_utc(self.finished_at),
            "duration_ms": self.duration_ms,
            "reviews_found": self.reviews_found,
            "new_reviews": self.new_reviews,
            "skipped_existing": self.skipped_existing,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "will_retry": self.will_retry,
            "fast_path": bool(self.fast_path),
        }
