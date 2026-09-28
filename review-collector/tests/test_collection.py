"""Tests 1-3 and 9: detection, de-duplication and failure isolation."""
from __future__ import annotations

import datetime as dt

from app.collector.base import AccessBlocked, RawReview
from app.database.database import session_scope
from app.database.models import Business, CheckRun, Review
from tests.conftest import business_id, make_review


# --------------------------------------------------------------- Test 1 + 2
def test_detects_new_bmw_review(fresh_db, service, stub_backend):
    """Test 1: a new BMW of Fort Walton Beach review is detected and stored."""
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]

    result = service.check_business(business_id("bmw_fwb"), trigger="test")

    assert result["status"] == "success"
    assert result["reviews_found"] == 1
    assert result["new_reviews"] == 1

    with session_scope() as session:
        row = session.query(Review).one()
        assert row.review_id == "bmw_fwb:greview-1"
        assert row.review_id_is_native is True
        assert row.reviewer_name == "Reviewer 1"
        assert row.rating == 5
        assert row.processed is False
        assert row.business.name == "BMW of Fort Walton Beach"


def test_detects_new_mercedes_review(fresh_db, service, stub_backend):
    """Test 2: a new Mercedes-Benz of Fort Walton Beach review is detected."""
    stub_backend.reviews_by_key["mb_fwb"] = [make_review(7)]

    result = service.check_business(business_id("mb_fwb"), trigger="test")

    assert result["status"] == "success"
    assert result["new_reviews"] == 1

    with session_scope() as session:
        row = session.query(Review).one()
        assert row.review_id == "mb_fwb:greview-7"
        assert row.business.name == "Mercedes-Benz of Fort Walton Beach"


def test_both_dealerships_are_independent(fresh_db, service, stub_backend):
    """The same Google review ID at two dealerships stays two distinct rows."""
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]
    stub_backend.reviews_by_key["mb_fwb"] = [make_review(1)]

    service.run_cycle(trigger="test")

    with session_scope() as session:
        ids = {row.review_id for row in session.query(Review).all()}
    assert ids == {"bmw_fwb:greview-1", "mb_fwb:greview-1"}


# --------------------------------------------------------------- Test 3
def test_running_twice_creates_no_duplicates(fresh_db, service, stub_backend):
    """Test 3: running the collector twice must not duplicate reviews."""
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(i) for i in range(1, 6)]

    first = service.check_business(business_id("bmw_fwb"), trigger="test")
    second = service.check_business(business_id("bmw_fwb"), trigger="test")

    assert first["new_reviews"] == 5
    assert second["new_reviews"] == 0
    assert second["skipped_existing"] == 5
    assert second["reviews_found"] == 5  # it did see them, it just didn't re-store them

    with session_scope() as session:
        assert session.query(Review).count() == 5


def test_new_review_appearing_later_is_picked_up(fresh_db, service, stub_backend):
    """Only the genuinely new review is added on a subsequent check."""
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1), make_review(2)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(3), make_review(1), make_review(2)]
    result = service.check_business(business_id("bmw_fwb"), trigger="test")

    assert result["new_reviews"] == 1
    assert result["skipped_existing"] == 2
    with session_scope() as session:
        assert session.query(Review).count() == 3


def test_reviews_without_google_ids_are_fingerprinted(fresh_db, service, stub_backend):
    """No stable Google ID -> a deterministic fingerprint still prevents duplicates."""
    review = make_review(1, native_id=False)
    stub_backend.reviews_by_key["bmw_fwb"] = [review]

    service.check_business(business_id("bmw_fwb"), trigger="test")
    second = service.check_business(business_id("bmw_fwb"), trigger="test")

    assert second["new_reviews"] == 0
    with session_scope() as session:
        row = session.query(Review).one()
        assert row.review_id.startswith("bmw_fwb:fp_")
        assert row.review_id_is_native is False


def test_duplicates_within_one_batch_are_collapsed(fresh_db, service, stub_backend):
    """Google rendering the same review twice mid-scroll must not double-insert."""
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1), make_review(1), make_review(2)]

    result = service.check_business(business_id("bmw_fwb"), trigger="test")

    assert result["new_reviews"] == 2
    with session_scope() as session:
        assert session.query(Review).count() == 2


def test_missing_fields_are_stored_as_null_not_invented(fresh_db, service, stub_backend):
    """Requirement 4: never invent data that Google did not publish."""
    stub_backend.reviews_by_key["bmw_fwb"] = [
        RawReview(
            source_review_id="sparse-1",
            reviewer_name=None,
            rating=4,
            review_text=None,      # a rating with no written comment is normal
            review_date=None,
            review_url=None,
            source="stub",
        )
    ]

    service.check_business(business_id("bmw_fwb"), trigger="test")

    with session_scope() as session:
        row = session.query(Review).one()
        assert row.rating == 4
        assert row.reviewer_name is None
        assert row.review_text is None
        assert row.review_date is None
        # review_url is DERIVED from Google's own review id, not invented, so it
        # is present even when the source published nothing else.
        assert "sparse-1" in row.review_url


# --------------------------------------------------------------- Test 9
def test_one_dealership_failing_does_not_stop_the_other(fresh_db, service, stub_backend):
    """Test 9: force BMW to fail; Mercedes-Benz must still be collected."""
    stub_backend.fail_keys["bmw_fwb"] = AccessBlocked(
        "Simulated failure: Google served the limited view."
    )
    stub_backend.reviews_by_key["mb_fwb"] = [make_review(11), make_review(12)]

    cycle = service.run_cycle(trigger="test")

    by_name = {r["business_name"]: r for r in cycle["results"]}
    bmw = by_name["BMW of Fort Walton Beach"]
    mercedes = by_name["Mercedes-Benz of Fort Walton Beach"]

    assert bmw["status"] == "failed"
    assert bmw["error_type"] == "AccessBlocked"
    assert bmw["will_retry"] is True          # reported honestly, retried next cycle
    assert mercedes["status"] == "success"    # unaffected
    assert mercedes["new_reviews"] == 2
    assert cycle["total_new_reviews"] == 2

    with session_scope() as session:
        assert session.query(Review).count() == 2
        failed = session.query(CheckRun).filter_by(status="failed").one()
        assert "limited view" in failed.error_message


def test_failure_is_recorded_with_diagnostic_detail(fresh_db, service, stub_backend):
    """Requirement 17: which business, what failed, why, when, and will it retry."""
    stub_backend.fail_keys["bmw_fwb"] = AccessBlocked("Google served a CAPTCHA.")

    service.check_business(business_id("bmw_fwb"), trigger="test")

    with session_scope() as session:
        run = session.query(CheckRun).order_by(CheckRun.id.desc()).first()
        assert run.status == "failed"
        assert run.business.name == "BMW of Fort Walton Beach"   # which
        assert run.error_type == "AccessBlocked"                 # what / why
        assert "CAPTCHA" in run.error_message
        assert run.started_at is not None and run.finished_at is not None  # when
        assert run.will_retry is True                            # will it retry


def test_no_reviews_are_created_when_collection_fails(fresh_db, service, stub_backend):
    """A failed check must never leave fabricated or partial data behind."""
    stub_backend.fail_keys["bmw_fwb"] = AccessBlocked("blocked")
    stub_backend.fail_keys["mb_fwb"] = AccessBlocked("blocked")

    cycle = service.run_cycle(trigger="test")

    assert all(r["status"] == "failed" for r in cycle["results"])
    with session_scope() as session:
        assert session.query(Review).count() == 0


# --------------------------------------------------------------- Initial sync
def test_initial_sync_marks_history_processed(fresh_db, stub_backend, monkeypatch):
    """Requirement 14: back-catalogue is stored but not pushed at A3."""
    from app.config import get_settings
    from app.services.collection_service import CollectionService

    settings = get_settings()
    monkeypatch.setattr(settings, "initial_sync", True)
    monkeypatch.setattr(settings, "initial_sync_mark_processed", True)
    service = CollectionService(settings)

    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(i) for i in range(1, 8)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    with session_scope() as session:
        assert session.query(Review).count() == 7
        assert session.query(Review).filter_by(processed=False).count() == 0
        assert session.query(Business).filter_by(key="bmw_fwb").one().initial_sync_done is True

    # After the initial sync, genuinely new reviews arrive unprocessed.
    stub_backend.reviews_by_key["bmw_fwb"].append(make_review(99))
    service.check_business(business_id("bmw_fwb"), trigger="test")

    with session_scope() as session:
        unprocessed = session.query(Review).filter_by(processed=False).all()
        assert len(unprocessed) == 1
        assert unprocessed[0].review_id == "bmw_fwb:greview-99"
