"""Pacing each dealership by how often it actually receives reviews.

Measured over 770 real checks, 96% found nothing. These tests pin the behaviour
that reclaims that: a busy dealership stays on the fast clock, a quiet one drops
back, and nothing is ever skipped in a way that could lose a review quietly.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.config import Settings
from app.database.database import session_scope
from app.database.models import Business, CheckRun, Review, utcnow
from app.services.cadence import (
    DORMANT, FAST, MEDIUM, SLOW, plan_for, review_velocity, stagger_offset, tier_for,
)


def settings(**over) -> Settings:
    base = dict(
        ADAPTIVE_INTERVAL_ENABLED="true",
        ADAPTIVE_FAST_PER_DAY="1.0",
        ADAPTIVE_MEDIUM_PER_DAY="0.2",
        ADAPTIVE_FAST_MINUTES="15",
        ADAPTIVE_MEDIUM_MINUTES="60",
        ADAPTIVE_SLOW_MINUTES="240",
        ADAPTIVE_DORMANT_PER_DAY="0.03",
        ADAPTIVE_DORMANT_MINUTES="720",
        ADAPTIVE_STAGGER_ENABLED="true",
        CHECK_INTERVAL_MINUTES="15",
        FULL_SCAN_EVERY_HOURS="24",
    )
    base.update(over)
    return Settings(**base)


def add_business(session, key="test_biz", *, synced=True) -> Business:
    b = Business(key=key, name=f"Dealer {key}", place_id="ChIJtest",
                 active=True, initial_sync_done=synced)
    session.add(b)
    session.flush()
    return b


def add_reviews(session, business_id, count, *, days_back_each=1.0, prefix="r"):
    for i in range(count):
        session.add(Review(
            business_id=business_id,
            review_id=f"{business_id}:{prefix}{i}",
            review_id_is_native=True,
            rating=5,
            review_text=f"Review {i}",
            review_date=utcnow() - dt.timedelta(days=i * days_back_each),
        ))
    session.flush()


def add_check(session, business_id, *, minutes_ago, fast_path=False, status="success"):
    session.add(CheckRun(
        business_id=business_id, trigger="scheduled", status=status,
        started_at=utcnow() - dt.timedelta(minutes=minutes_ago),
        fast_path=fast_path,
    ))
    session.flush()


class TestTiers:
    def test_a_dealership_with_a_review_a_day_stays_on_the_fast_clock(self):
        assert tier_for(2.0, settings()) == (FAST, 15)
        assert tier_for(1.0, settings()) == (FAST, 15)

    def test_a_quieter_one_moves_to_hourly(self):
        # Mercedes-Benz FWB: 14 reviews in 30 days, about 0.47 a day.
        assert tier_for(0.47, settings()) == (MEDIUM, 60)

    def test_a_very_quiet_one_moves_to_four_hourly(self):
        # About one review a fortnight.
        assert tier_for(0.07, settings()) == (SLOW, 240)

    def test_a_dealership_with_almost_no_reviews_goes_dormant(self):
        # Under one review a month. At any real customer count most dealerships
        # land here, and they are why 500 is affordable at all.
        assert tier_for(0.02, settings()) == (DORMANT, 720)
        assert tier_for(0.0, settings()) == (DORMANT, 720)


class TestStagger:
    def test_two_dealerships_on_the_same_clock_get_different_offsets(self):
        offsets = {stagger_offset(f"dealer-{i}", 240, 15) for i in range(40)}
        # 240/15 gives 16 possible slots; 40 dealerships should fill most of them.
        assert len(offsets) >= 10

    def test_an_offset_never_moves_for_the_same_dealership(self):
        first = stagger_offset("bmw-fwb", 240, 15)
        assert all(stagger_offset("bmw-fwb", 240, 15) == first for _ in range(5))

    def test_offsets_land_on_scheduler_ticks_inside_the_interval(self):
        for i in range(60):
            off = stagger_offset(f"d{i}", 240, 15)
            assert 0 <= off < 240
            assert off % 15 == 0

    def test_a_fast_clock_cannot_be_staggered_below_one_tick(self):
        # 15 minute interval on a 15 minute tick leaves exactly one slot.
        assert stagger_offset("anything", 15, 15) == 0

    def test_the_load_spreads_rather_than_arriving_in_one_burst(self):
        # The point of the whole exercise: 200 dealerships on the four hourly
        # clock must not all come due on the same tick.
        buckets = {}
        for i in range(200):
            off = stagger_offset(f"dealer-{i}", 240, 15)
            buckets[off] = buckets.get(off, 0) + 1
        busiest = max(buckets.values())
        # Without staggering this would be 200. Evenly spread it is about 12.
        assert busiest < 40, f"worst tick holds {busiest} of 200"


class TestVelocity:
    def test_counts_only_the_recent_window(self, fresh_db):
        with session_scope() as session:
            b = add_business(session)
            add_reviews(session, b.id, 10, days_back_each=1)    # inside 30 days
            add_reviews(session, b.id, 5, days_back_each=100, prefix="old")  # long ago
            v = review_velocity(session, b.id, days=30)
        # The old ones must not make a quiet dealership look busy.
        assert 0.3 < v < 0.4

    def test_a_dealership_with_no_reviews_reads_as_zero(self, fresh_db):
        with session_scope() as session:
            b = add_business(session)
            assert review_velocity(session, b.id, days=30) == 0.0


class TestDue:
    def test_a_dealership_never_collected_from_is_always_due(self, fresh_db):
        with session_scope() as session:
            b = add_business(session, synced=False)
            plan = plan_for(session, b, settings())
        assert plan["due"] is True
        assert plan["full_scan"] is True
        assert "first collection" in plan["reason"]

    def test_a_busy_dealership_is_due_again_after_fifteen_minutes(self, fresh_db):
        with session_scope() as session:
            b = add_business(session)
            add_reviews(session, b.id, 60, days_back_each=0.5)   # 2 a day
            add_check(session, b.id, minutes_ago=16)
            plan = plan_for(session, b, settings())
        assert plan["tier"] == FAST
        assert plan["due"] is True

    def test_a_busy_dealership_is_not_re_checked_after_five_minutes(self, fresh_db):
        with session_scope() as session:
            b = add_business(session)
            add_reviews(session, b.id, 60, days_back_each=0.5)
            add_check(session, b.id, minutes_ago=5)
            plan = plan_for(session, b, settings())
        assert plan["due"] is False

    def test_a_quiet_dealership_waits_an_hour_rather_than_fifteen_minutes(self, fresh_db):
        with session_scope() as session:
            b = add_business(session)
            add_reviews(session, b.id, 14, days_back_each=2)     # about 0.47 a day
            check = CheckRun(business_id=b.id, trigger="scheduled", status="success",
                             started_at=utcnow() - dt.timedelta(minutes=20))
            session.add(check)
            session.flush()

            plan = plan_for(session, b, settings())
            assert plan["tier"] == MEDIUM
            # The whole saving: on the old 15 minute clock it would be checked here.
            assert plan["due"] is False

            check.started_at = utcnow() - dt.timedelta(minutes=61)
            session.flush()
            assert plan_for(session, b, settings())["due"] is True

    def test_a_window_boundary_alone_does_not_trigger_a_second_check(self, fresh_db):
        # Staggering makes a dealership due when the clock crosses into its own
        # window. Without a floor, a boundary landing just after a check means
        # two checks a few minutes apart.
        with session_scope() as session:
            b = add_business(session, key="boundary_biz")
            add_reviews(session, b.id, 60, days_back_each=0.5)
            for minutes in (1, 3, 6):
                check = CheckRun(business_id=b.id, trigger="scheduled", status="success",
                                 started_at=utcnow() - dt.timedelta(minutes=minutes))
                session.add(check)
                session.flush()
                assert plan_for(session, b, settings())["due"] is False
                session.delete(check)
                session.flush()

    def test_turning_pacing_off_makes_everything_due(self, fresh_db):
        with session_scope() as session:
            b = add_business(session)
            add_check(session, b.id, minutes_ago=1)
            plan = plan_for(session, b, settings(ADAPTIVE_INTERVAL_ENABLED="false"))
        assert plan["due"] is True

    def test_a_failed_check_does_not_count_as_having_been_checked(self, fresh_db):
        # Otherwise a dealership that keeps failing would be quietly left alone.
        with session_scope() as session:
            b = add_business(session)
            add_reviews(session, b.id, 60, days_back_each=0.5)
            add_check(session, b.id, minutes_ago=2, status="failed")
            plan = plan_for(session, b, settings())
        assert plan["due"] is True


class TestFullScan:
    def test_a_full_read_is_forced_when_the_last_one_is_old(self, fresh_db):
        with session_scope() as session:
            b = add_business(session)
            add_check(session, b.id, minutes_ago=60 * 30, fast_path=False)  # 30h ago
            add_check(session, b.id, minutes_ago=20, fast_path=True)
            plan = plan_for(session, b, settings())
        assert plan["full_scan"] is True

    def test_a_recent_full_read_allows_the_fast_path(self, fresh_db):
        with session_scope() as session:
            b = add_business(session)
            add_check(session, b.id, minutes_ago=90, fast_path=False)
            plan = plan_for(session, b, settings())
        assert plan["full_scan"] is False

    def test_fast_checks_alone_never_satisfy_the_full_scan_clock(self, fresh_db):
        # A fast check reads a count and stops, so it cannot notice a review that
        # was edited or deleted without changing the total.
        with session_scope() as session:
            b = add_business(session)
            for minutes in (300, 200, 100, 30):
                add_check(session, b.id, minutes_ago=minutes, fast_path=True)
            plan = plan_for(session, b, settings())
        assert plan["full_scan"] is True
