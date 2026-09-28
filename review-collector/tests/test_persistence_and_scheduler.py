"""Tests 8, 10 and 12: restart persistence, Check Now, and the scheduler."""
from __future__ import annotations

import time

from app.database.database import reset_engine, session_scope
from app.database.models import Review
from tests.conftest import business_id, make_review


# --------------------------------------------------------------- Test 8
def test_reviews_survive_an_application_restart(fresh_db, service, stub_backend):
    """Test 8: restart the app; stored reviews must still be there."""
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(i) for i in range(1, 5)]
    stub_backend.reviews_by_key["mb_fwb"] = [make_review(i) for i in range(20, 23)]
    service.run_cycle(trigger="test")

    with session_scope() as session:
        before = session.query(Review).count()
        ids_before = {row.review_id for row in session.query(Review).all()}
        session.query(Review).filter_by(review_id="bmw_fwb:greview-1").one().processed = True
    assert before == 7

    # Simulate a full process restart: dispose the engine and reconnect to the
    # same SQLite file, exactly as a fresh boot would.
    reset_engine()
    from app.database.database import init_db, sync_businesses_from_config

    init_db()
    sync_businesses_from_config()

    with session_scope() as session:
        assert session.query(Review).count() == before
        assert {row.review_id for row in session.query(Review).all()} == ids_before
        # Processed state survives too, so A3 is not re-sent old reviews.
        assert session.query(Review).filter_by(review_id="bmw_fwb:greview-1").one().processed is True


def test_no_duplicates_after_a_restart(fresh_db, service, stub_backend):
    """Restarting and re-checking must not re-insert everything."""
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(i) for i in range(1, 4)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    reset_engine()
    from app.database.database import init_db

    init_db()

    result = service.check_business(business_id("bmw_fwb"), trigger="test")
    assert result["new_reviews"] == 0
    with session_scope() as session:
        assert session.query(Review).count() == 3


def test_unique_constraint_is_enforced_by_the_database(fresh_db, service, stub_backend):
    """Requirement 5/6: the DB itself refuses duplicate review_ids."""
    from sqlalchemy.exc import IntegrityError

    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    raised = False
    try:
        with session_scope() as session:
            existing = session.query(Review).one()
            session.add(
                Review(
                    business_id=existing.business_id,
                    review_id=existing.review_id,  # deliberate collision
                    rating=1,
                )
            )
    except IntegrityError:
        raised = True

    assert raised, "the UNIQUE constraint on reviews.review_id did not fire"
    with session_scope() as session:
        assert session.query(Review).count() == 1


# --------------------------------------------------------------- Test 10
def test_check_now_button_triggers_both_dealerships(client, auth, stub_backend, monkeypatch):
    """Test 10: the dashboard's [Check Now] button checks both businesses."""
    from app.scheduler.scheduler import get_scheduler, reset_scheduler

    reset_scheduler()
    scheduler = get_scheduler()
    monkeypatch.setattr(
        "app.services.collection_service.get_backends", lambda settings: [stub_backend]
    )
    scheduler.service.settings = scheduler.settings

    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]
    stub_backend.reviews_by_key["mb_fwb"] = [make_review(2)]

    response = client.post("/api/admin/check-now", headers=auth)

    assert response.status_code == 200
    body = response.json()
    assert body["skipped"] is False
    assert body["trigger"] == "manual"
    assert len(body["results"]) == 2
    assert sorted(stub_backend.calls) == ["bmw_fwb", "mb_fwb"]
    assert body["total_new_reviews"] == 2

    # The collected reviews are immediately available to A3.
    assert client.get("/api/reviews/new", headers=auth).json()["count"] == 2
    reset_scheduler()


def test_check_now_reports_a_failing_dealership_honestly(client, auth, stub_backend, monkeypatch):
    """The button must surface failure, not pretend the check succeeded."""
    from app.collector.base import AccessBlocked
    from app.scheduler.scheduler import get_scheduler, reset_scheduler

    reset_scheduler()
    get_scheduler()
    monkeypatch.setattr(
        "app.services.collection_service.get_backends", lambda settings: [stub_backend]
    )
    stub_backend.fail_keys["bmw_fwb"] = AccessBlocked("Google served the limited view.")
    stub_backend.reviews_by_key["mb_fwb"] = [make_review(5)]

    body = client.post("/api/admin/check-now", headers=auth).json()

    by_name = {r["business_name"]: r for r in body["results"]}
    assert by_name["BMW of Fort Walton Beach"]["status"] == "failed"
    assert "limited view" in by_name["BMW of Fort Walton Beach"]["error_message"]
    assert by_name["Mercedes-Benz of Fort Walton Beach"]["status"] == "success"
    reset_scheduler()


# --------------------------------------------------------------- Test 12
def test_scheduler_starts_stops_and_reports_next_run(fresh_db):
    """Test 12: the automatic scheduler runs on the configured interval."""
    from app.scheduler.scheduler import ReviewScheduler

    scheduler = ReviewScheduler()
    try:
        assert scheduler.running is False
        assert scheduler.next_run_time() is None

        status = scheduler.start()
        assert status["running"] is True
        assert status["interval_minutes"] == 15
        assert status["next_check"] is not None

        job = scheduler._scheduler.get_job("review_check")
        assert job is not None
        assert int(job.trigger.interval.total_seconds()) == 15 * 60

        assert scheduler.stop()["running"] is False
        assert scheduler.restart()["running"] is True
    finally:
        scheduler.stop()


def test_scheduler_actually_fires_the_collection_job(fresh_db, stub_backend, monkeypatch):
    """Prove the job really runs on its interval, not just that it is registered."""
    import datetime as dt

    from apscheduler.triggers.interval import IntervalTrigger

    from app.scheduler.scheduler import ReviewScheduler

    monkeypatch.setattr(
        "app.services.collection_service.get_backends", lambda settings: [stub_backend]
    )
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]
    stub_backend.reviews_by_key["mb_fwb"] = [make_review(2)]

    scheduler = ReviewScheduler()
    try:
        # Same code path as production, just a 1-second interval so the test is quick.
        scheduler._scheduler.add_job(
            scheduler._job,
            trigger=IntervalTrigger(seconds=1),
            id="review_check",
            replace_existing=True,
            next_run_time=dt.datetime.now(dt.timezone.utc),
        )
        scheduler._scheduler.start()

        deadline = time.time() + 20
        while time.time() < deadline and not scheduler.last_result:
            time.sleep(0.25)

        assert scheduler.last_result is not None, "the scheduled job never ran"
        assert scheduler.last_result["skipped"] is False
        assert len(scheduler.last_result["results"]) == 2
        with session_scope() as session:
            assert session.query(Review).count() == 2
    finally:
        scheduler.stop()


def test_overlapping_cycles_are_skipped_not_doubled(fresh_db, service, stub_backend):
    """A manual check during a scheduled one must not double-collect."""
    import threading

    from app.services.collection_service import _cycle_lock

    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]

    _cycle_lock.acquire()
    try:
        result = service.run_cycle(trigger="manual")
    finally:
        _cycle_lock.release()

    assert result["skipped"] is True
    assert "already in progress" in result["reason"]
    with session_scope() as session:
        assert session.query(Review).count() == 0


def test_scheduler_status_endpoints(client, auth):
    from app.scheduler.scheduler import reset_scheduler

    reset_scheduler()
    assert client.post("/api/admin/scheduler/start", headers=auth).json()["running"] is True
    assert client.post("/api/admin/scheduler/restart", headers=auth).json()["running"] is True
    assert client.post("/api/admin/scheduler/stop", headers=auth).json()["running"] is False
    reset_scheduler()


def test_interrupted_checks_are_closed_on_startup(fresh_db):
    """A process killed mid-check must not leave a run 'running' forever."""
    from app.database.database import close_orphaned_checks
    from app.database.models import Business, CheckRun

    with session_scope() as session:
        business = session.query(Business).first()
        session.add(CheckRun(business_id=business.id, trigger="scheduled", status="running"))

    close_orphaned_checks()

    with session_scope() as session:
        run = session.query(CheckRun).one()
        assert run.status == "failed"
        assert run.error_type == "Interrupted"
        assert run.finished_at is not None
        assert run.will_retry is True
