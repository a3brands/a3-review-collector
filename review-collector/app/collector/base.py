"""Collector contract.

Everything Google-specific lives behind this interface. The API, database,
scheduler and dashboard only ever see ``RawReview`` objects, so the whole
collection layer can be swapped out without touching the rest of the app.
"""
from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
class CollectorError(Exception):
    """Base class for every collection failure.

    ``retryable`` tells the service layer whether a later scheduled run has a
    realistic chance of succeeding, which is what we report to the user.
    """

    retryable = True

    def __init__(self, message: str, *, retryable: Optional[bool] = None) -> None:
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class BackendUnavailable(CollectorError):
    """The backend cannot run at all (missing credentials, browser not installed)."""

    retryable = False


class AccessBlocked(CollectorError):
    """Google served a CAPTCHA, a consent wall, or a review-stripped page.

    This is deliberately NOT worked around -- we surface it instead. Retrying
    later can help, so it stays retryable.
    """

    retryable = True


class TransientCollectorError(CollectorError):
    """Timeout, network error, 5xx -- worth retrying."""

    retryable = True


class ParseError(CollectorError):
    """Page loaded but the structure was not what we expected.

    Usually means Google changed its markup and parser.py needs updating.
    """

    retryable = True


# --------------------------------------------------------------------------
# Data carried out of the collection layer
# --------------------------------------------------------------------------
@dataclass
class RawReview:
    """One review exactly as the source gave it to us.

    Every field is Optional on purpose. We never invent data: if Google did not
    publish it, it stays ``None`` all the way through to the API.
    """

    source_review_id: Optional[str] = None
    reviewer_name: Optional[str] = None
    reviewer_profile_url: Optional[str] = None
    rating: Optional[int] = None
    review_text: Optional[str] = None
    review_date: Optional[dt.datetime] = None
    review_date_is_approximate: bool = False
    review_date_raw: Optional[str] = None
    review_url: Optional[str] = None
    # The dealership's own reply. Collected for A3 (which uses it to decide
    # whether a review still needs a draft) but never shown in notification
    # emails, where only the customer's comment belongs.
    owner_replied: bool = False
    owner_reply: Optional[str] = None
    owner_reply_date: Optional[dt.datetime] = None
    source: str = "unknown"

    def has_content(self) -> bool:
        """A review we can meaningfully store: at minimum a rating or some text."""
        return self.rating is not None or bool((self.review_text or "").strip())


@dataclass
class CollectionResult:
    reviews: List[RawReview] = field(default_factory=list)
    backend: str = "unknown"
    total_review_count: Optional[int] = None
    average_rating: Optional[float] = None
    notes: List[str] = field(default_factory=list)
    # True when the backend read the listing's headline count, found it
    # unchanged, and stopped without reading the reviews. An empty `reviews`
    # then means "nothing new", not "nothing found".
    skipped_unchanged: bool = False
    # Set when the backend had to work out the real listing for itself, because
    # what was configured was a search URL or a business name. The caller reports
    # it back so the dealership stops being a guess.
    resolved_url: Optional[str] = None
    resolved_name: Optional[str] = None
    # The address the listing actually loaded at. A place_id link redirects to
    # a full /maps/place/ URL carrying the feature id that review links need.
    listing_url: Optional[str] = None


# --------------------------------------------------------------------------
# Backend interface
# --------------------------------------------------------------------------
class CollectorBackend(ABC):
    name: str = "base"

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """(usable, human readable reason). Checked before every run."""

    @abstractmethod
    def collect(self, business, limit: int,
                known_review_count: Optional[int] = None) -> CollectionResult:
        """Fetch up to ``limit`` most recent reviews for ``business``.

        ``known_review_count`` is the listing total already on file. A backend
        that can read the live total cheaply may compare the two and return
        early with ``skipped_unchanged=True`` rather than reading every review.
        Backends that cannot are free to ignore it.

        ``business`` is an ``app.database.models.Business`` row.
        Must raise a ``CollectorError`` subclass on failure -- never return
        fabricated or partial-but-unmarked data.
        """

    def close(self) -> None:  # pragma: no cover - optional hook
        """Release any resources (browser processes, HTTP clients)."""
