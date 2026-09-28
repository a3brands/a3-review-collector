"""The alarm for collection that has silently stopped.

Every other failure here announces itself. This one does not: the page loads,
the check reports success with zero found, and the dashboard looks healthy while
nothing is being collected. These tests pin the one signature that catches it,
and the several situations that must NOT set it off.
"""
from __future__ import annotations

import datetime as dt

from app.config import Settings
from app.database.database import session_scope
from app.database.models import Business, CheckRun, Review, utcnow
from app.services.watchdog import assess


def settings(**over) -> Settings:
    base = dict(STALL_ALERT_ENABLED="true", STALL_ALERT_HOURS="48")
    base.update(over)
    return Settings(**base)


def isolate(session) -> None:
    """Deactivate whatever the fixture seeded.

    fresh_db registers the two configured dealerships, so a test that adds one
    business is actually looking at three. The watchdog counts across every
    active dealership by design, so the tests have to control the whole roster.
    """
    for b in session.query(Business).all():
        b.active = False
    session.flush()


def business(session, key="biz", *, active=True) -> Business:
    b = Business(key=key, name=f"Dealer {key}", place_id="ChIJtest",
                 active=active, initial_sync_done=True)
    session.add(b)
    session.flush()
    return b


def review(session, business_id, *, hours_ago: float, ident: str) -> None:
    session.add(Review(
        business_id=business_id, review_id=ident, review_id_is_native=True,
        rating=5, review_text="A review",
        review_date=utcnow() - dt.timedelta(hours=hours_ago),
        detected_at=utcnow() - dt.timedelta(hours=hours_ago),
    ))
    session.flush()


def check(session, business_id, *, hours_ago: float, status="success") -> None:
    session.add(CheckRun(
        business_id=business_id, trigger="scheduled", status=status,
        started_at=utcnow() - dt.timedelta(hours=hours_ago),
    ))
    session.flush()


class TestItFires:
    def test_no_reviews_for_two_days_while_checks_report_success(self, fresh_db):
        # The signature. Checks are running, nothing is complaining, and nothing
        # has arrived. That combination is the silent failure.
        with session_scope() as session:
            isolate(session)
            b = business(session)
            review(session, b.id, hours_ago=60, ident="old-1")
            for hours in (40, 30, 20, 10, 1):
                check(session, b.id, hours_ago=hours)

            verdict = assess(session, settings())

        assert verdict["stalled"] is True
        assert verdict["quiet_hours"] >= 48
        assert "reported success" in verdict["reason"]

    def test_it_looks_across_every_dealership_not_one(self, fresh_db):
        # One quiet dealership is normal. Every dealership quiet is not.
        with session_scope() as session:
            isolate(session)
            a = business(session, "a")
            b = business(session, "b")
            review(session, a.id, hours_ago=60, ident="a-old")
            review(session, b.id, hours_ago=55, ident="b-old")
            check(session, a.id, hours_ago=2)
            check(session, b.id, hours_ago=2)

            assert assess(session, settings())["stalled"] is True

            # One review from either of them clears it.
            review(session, b.id, hours_ago=1, ident="b-new")
            assert assess(session, settings())["stalled"] is False


class TestItStaysQuiet:
    def test_a_recent_review_is_not_a_stall(self, fresh_db):
        with session_scope() as session:
            isolate(session)
            b = business(session)
            review(session, b.id, hours_ago=3, ident="r1")
            check(session, b.id, hours_ago=1)
            assert assess(session, settings())["stalled"] is False

    def test_a_collector_that_is_not_running_is_a_different_alarm(self, fresh_db):
        # No successful checks means the collector is down, which announces
        # itself elsewhere. Firing this alert too would send two alarms for one
        # problem and point at the wrong cause.
        with session_scope() as session:
            isolate(session)
            b = business(session)
            review(session, b.id, hours_ago=90, ident="r1")
            verdict = assess(session, settings())
        assert verdict["stalled"] is False

    def test_checks_that_are_all_failing_do_not_trigger_it(self, fresh_db):
        with session_scope() as session:
            isolate(session)
            b = business(session)
            review(session, b.id, hours_ago=90, ident="r1")
            for hours in (20, 10, 2):
                check(session, b.id, hours_ago=hours, status="failed")
            assert assess(session, settings())["stalled"] is False

    def test_a_fresh_install_with_nothing_collected_is_not_a_stall(self, fresh_db):
        with session_scope() as session:
            isolate(session)
            b = business(session)
            check(session, b.id, hours_ago=1)
            verdict = assess(session, settings())
        assert verdict["stalled"] is False
        assert "nothing collected yet" in verdict["reason"]

    def test_no_active_dealerships_is_not_a_stall(self, fresh_db):
        with session_scope() as session:
            isolate(session)
            b = business(session, active=False)
            review(session, b.id, hours_ago=200, ident="r1")
            check(session, b.id, hours_ago=1)
            verdict = assess(session, settings())
        assert verdict["stalled"] is False
        assert "no active dealerships" in verdict["reason"]


class TestThreshold:
    def test_the_window_is_configurable(self, fresh_db):
        with session_scope() as session:
            isolate(session)
            b = business(session)
            review(session, b.id, hours_ago=10, ident="r1")
            check(session, b.id, hours_ago=1)

            assert assess(session, settings(STALL_ALERT_HOURS="48"))["stalled"] is False
            # BMW alone averages two reviews a day, so a tight window suits a
            # busy roster and the setting exists to allow it.
            assert assess(session, settings(STALL_ALERT_HOURS="6"))["stalled"] is True

    def test_the_verdict_explains_itself(self, fresh_db):
        with session_scope() as session:
            isolate(session)
            b = business(session)
            review(session, b.id, hours_ago=72, ident="r1")
            check(session, b.id, hours_ago=1)
            verdict = assess(session, settings())

        # An alert that cannot say why it fired gets muted rather than acted on.
        assert verdict["active_businesses"] == 1
        assert verdict["successful_checks_in_window"] == 1
        assert verdict["threshold_hours"] == 48
        assert str(verdict["quiet_hours"]) in verdict["reason"] or "72" in verdict["reason"]
