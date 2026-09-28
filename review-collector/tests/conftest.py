"""Test fixtures.

These tests exercise the collector's own logic -- de-duplication, storage,
persistence, the REST API, auth, failure isolation and the scheduler -- using a
temporary SQLite database and a stub backend that stands in for Google.

The stub is a TEST DOUBLE for the network, not fake product data: no fixture
review ever reaches a real database or the running service. The tests that talk
to the real Google Business Profiles live in tests/test_live_collection.py and
are skipped unless you opt in with RUN_LIVE_TESTS=1.
"""
from __future__ import annotations

import datetime as dt
import os
import tempfile
from pathlib import Path

import pytest

TMP = Path(tempfile.mkdtemp(prefix="a3collector-test-"))

os.environ.update(
    {
        "ENV_FILE": str(TMP / "nonexistent.env"),  # ignore the developer's real .env
        "DATABASE_URL": f"sqlite:///{TMP / 'test.db'}",
        "LOG_FILE": str(TMP / "test.log"),
        "A3_API_KEY": "test-key-do-not-use-in-production",
        "SCHEDULER_ENABLED": "false",
        "CHECK_INTERVAL_MINUTES": "15",
        "INITIAL_SYNC": "false",
        "COLLECTOR_BACKEND": "playwright",
        "COLLECTOR_MAX_RETRIES": "0",
        "COLLECTOR_RETRY_BACKOFF_SECONDS": "0",
        # Tests must not sleep for a random interval before doing their work.
        # The jitter exists to stop several real browsers hitting Google at the
        # same instant; a stub backend has nobody to be polite to.
        "COLLECTOR_WORKER_JITTER_SECONDS": "0",
        "STARTUP_CHECK_DELAY_SECONDS": "1",
        "BMW_FWB_PLACE_ID": "ChIJ0XFsWkI-kYgR1j9GfmxgynY",
        "MB_FWB_PLACE_ID": "ChIJ8SSirEM-kYgRPatxgc1nH88",
    }
)

from app.collector.base import (  # noqa: E402
    AccessBlocked,
    CollectionResult,
    CollectorBackend,
    RawReview,
)
from app.config import get_settings, reset_settings_cache  # noqa: E402
from app.database.database import (  # noqa: E402
    init_db,
    reset_engine,
    session_scope,
    sync_businesses_from_config,
)
from app.database.models import Base, Business, CheckRun, Review  # noqa: E402


class StubBackend(CollectorBackend):
    """Stands in for Google so collection logic can be tested deterministically."""

    name = "stub"

    def __init__(self) -> None:
        self.reviews_by_key: dict[str, list[RawReview]] = {}
        self.fail_keys: dict[str, Exception] = {}
        self.available = True
        self.calls: list[str] = []
        self.known_counts: list = []

    def is_available(self):
        return (self.available, "stub backend" if self.available else "stub disabled")

    def collect(self, business, limit, known_review_count=None):
        # The known count is recorded rather than acted on: these tests are about
        # what the service does with a result, and a stub that decided to skip
        # would be testing the stub's judgement instead.
        self.calls.append(business.key)
        self.known_counts.append(known_review_count)
        if business.key in self.fail_keys:
            raise self.fail_keys[business.key]
        return CollectionResult(
            reviews=list(self.reviews_by_key.get(business.key, []))[:limit],
            backend=self.name,
        )


def make_review(idx: int, *, native_id: bool = True, **overrides) -> RawReview:
    data = dict(
        source_review_id=(overrides.pop("source_review_id", None) or f"greview-{idx}")
        if native_id else None,
        reviewer_name=f"Reviewer {idx}",
        reviewer_profile_url=f"https://www.google.com/maps/contrib/{idx}",
        rating=5,
        review_text=f"Review body number {idx}.",
        review_date=dt.datetime(2026, 8, 1) + dt.timedelta(days=idx),
        review_url="https://maps.google.com/",
        source="stub",
    )
    data.update(overrides)
    return RawReview(**data)


@pytest.fixture
def fresh_db():
    """A clean database for each test."""
    reset_settings_cache()
    reset_engine()
    db_file = TMP / "test.db"
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db_file) + suffix)
        if path.exists():
            path.unlink()
    init_db()
    sync_businesses_from_config()
    yield
    reset_engine()


@pytest.fixture
def stub_backend(monkeypatch):
    backend = StubBackend()
    monkeypatch.setattr("app.services.collection_service.get_backends", lambda settings: [backend])
    return backend


@pytest.fixture
def service(stub_backend):
    from app.services.collection_service import CollectionService

    reset_settings_cache()
    return CollectionService(get_settings())


@pytest.fixture
def client(fresh_db):
    from fastapi.testclient import TestClient

    from app.main import app

    # Exercise the routes without the lifespan starting a real scheduler/browser.
    app.router.lifespan_context = _null_lifespan
    with TestClient(app) as test_client:
        yield test_client


from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _null_lifespan(app):
    yield


@pytest.fixture
def auth():
    return {"Authorization": "Bearer test-key-do-not-use-in-production"}


def business_id(key: str) -> int:
    with session_scope() as session:
        return session.query(Business).filter_by(key=key).one().id
