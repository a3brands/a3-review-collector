"""Collecting for a dealership the moment it is added.

Somebody adds a customer at /admin and reasonably expects reviews, not a
fifteen minute wait in front of an empty queue with nothing saying a check is
coming. The paced cycle is right for established dealerships and much too slow
for one somebody is watching.

What must not happen is the opposite mistake: this running against dealerships
that are already collecting, which would undo the pacing entirely and put every
dealership back on a one minute clock.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.database.database import session_scope
from app.database.models import Business, CheckRun, utcnow


@pytest.fixture(autouse=True)
def no_config_resync(monkeypatch):
    """Stop onboard_new re-adding the dealerships configured in .env.

    It refreshes the roster first, which is right in production and wrong here:
    it would re-register and reactivate the two configured dealerships and every
    test below would be looking at three dealerships instead of its own.
    """
    monkeypatch.setattr(
        "app.services.collection_service.sync_businesses_from_config",
        lambda: None,
    )


def isolate(session) -> None:
    for b in session.query(Business).all():
        b.active = False
    session.flush()


def business(session, key, *, active=True) -> Business:
    b = Business(key=key, name=f"Dealer {key}", place_id="ChIJtest",
                 active=active, initial_sync_done=True)
    session.add(b)
    session.flush()
    return b


def check(session, business_id, *, status="success", minutes_ago=5) -> None:
    session.add(CheckRun(
        business_id=business_id, trigger="scheduled", status=status,
        started_at=utcnow() - dt.timedelta(minutes=minutes_ago),
    ))
    session.flush()


def test_a_dealership_never_collected_for_is_picked_up(service, stub_backend, fresh_db):
    with session_scope() as session:
        isolate(session)
        business(session, "brand_new")

    out = service.onboard_new()

    assert "Dealer brand_new" in (out["onboarded"] or [])
    assert "brand_new" in stub_backend.calls


def test_a_dealership_already_collecting_is_left_alone(service, stub_backend, fresh_db):
    # The pacing exists because 96% of checks find nothing. Re-checking an
    # established dealership every minute would throw that away.
    with session_scope() as session:
        isolate(session)
        b = business(session, "established")
        check(session, b.id, status="success")

    out = service.onboard_new()

    assert out["onboarded"] == []
    assert stub_backend.calls == []


def test_a_dealership_whose_only_checks_failed_is_still_new(service, stub_backend, fresh_db):
    # It has never actually collected anything, so it still counts as waiting to
    # start rather than as established.
    with session_scope() as session:
        isolate(session)
        b = business(session, "failing")
        check(session, b.id, status="failed")

    out = service.onboard_new()

    assert "Dealer failing" in (out["onboarded"] or [])


def test_an_inactive_dealership_is_not_collected_for(service, stub_backend, fresh_db):
    with session_scope() as session:
        isolate(session)
        business(session, "switched_off", active=False)

    out = service.onboard_new()

    assert out["onboarded"] == []
    assert stub_backend.calls == []


def test_nothing_new_means_nothing_happens(service, stub_backend, fresh_db):
    # The normal case, running every minute. It must be cheap and silent.
    with session_scope() as session:
        isolate(session)

    out = service.onboard_new()

    assert out["onboarded"] == []
    assert stub_backend.calls == []


def test_several_new_dealerships_are_all_picked_up(service, stub_backend, fresh_db):
    with session_scope() as session:
        isolate(session)
        for key in ("one", "two", "three"):
            business(session, key)

    out = service.onboard_new()

    assert len(out["onboarded"]) == 3
    assert sorted(stub_backend.calls) == ["one", "three", "two"]


def test_one_failing_dealership_does_not_stop_the_others(service, stub_backend, fresh_db):
    from app.collector.base import AccessBlocked

    with session_scope() as session:
        isolate(session)
        for key in ("good_one", "bad_one"):
            business(session, key)

    stub_backend.fail_keys["bad_one"] = AccessBlocked("blocked on purpose")

    out = service.onboard_new()

    # Both attempted, and the good one collected.
    assert sorted(stub_backend.calls) == ["bad_one", "good_one"]
    assert len(out["onboarded"]) == 2
