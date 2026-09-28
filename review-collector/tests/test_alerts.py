"""Event-driven alerts: new review, failure, recovery.

These are the notifications that arrive when nobody is watching the dashboard.
They were previously dead config -- the settings existed and were documented,
but no code ever emitted the events.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.collector.base import AccessBlocked
from app.config import get_settings
from tests.conftest import business_id, make_review


@pytest.fixture
def sent(monkeypatch):
    """Capture notifier.send_event calls made by the collection service."""
    calls = []

    def fake(event, **kwargs):
        calls.append({"event": event, **kwargs})
        return {"sent": True}

    monkeypatch.setattr("app.services.collection_service.notifier.send_event", fake)
    return calls


def _events(calls, name):
    return [c for c in calls if c["event"] == name]


# --------------------------------------------------------------- new review
def test_new_review_alert_fires_with_the_review_content(fresh_db, service, stub_backend, sent):
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1), make_review(2)]

    service.check_business(business_id("bmw_fwb"), trigger="test")

    alerts = _events(sent, "new_review")
    assert len(alerts) == 1
    reviews = alerts[0]["reviews"]
    assert len(reviews) == 2
    assert {r["reviewer_name"] for r in reviews} == {"Reviewer 1", "Reviewer 2"}
    assert alerts[0]["results"][0]["business_name"] == "BMW of Fort Walton Beach"


def test_no_new_review_alert_when_nothing_is_new(fresh_db, service, stub_backend, sent):
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]
    service.check_business(business_id("bmw_fwb"), trigger="test")
    sent.clear()

    service.check_business(business_id("bmw_fwb"), trigger="test")   # same reviews

    assert _events(sent, "new_review") == []


def test_initial_sync_does_not_alert_on_the_whole_back_catalogue(
    fresh_db, stub_backend, sent, monkeypatch
):
    """200 historical reviews must not produce a 200-review alert."""
    from app.services.collection_service import CollectionService

    settings = get_settings()
    monkeypatch.setattr(settings, "initial_sync", True)
    monkeypatch.setattr(settings, "initial_sync_mark_processed", True)
    service = CollectionService(settings)

    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(i) for i in range(1, 30)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    assert _events(sent, "new_review") == []

    # ...but a genuinely new one afterwards does alert. Inserted at the front
    # because collection sorts newest-first, and only the newest N are read.
    stub_backend.reviews_by_key["bmw_fwb"].insert(0, make_review(99))
    service.check_business(business_id("bmw_fwb"), trigger="test")
    assert len(_events(sent, "new_review")) == 1


# --------------------------------------------------------------- failure
def test_failure_alert_fires_once_not_every_cycle(fresh_db, service, stub_backend, sent):
    stub_backend.fail_keys["bmw_fwb"] = AccessBlocked("Google served the reduced listing.")

    service.check_business(business_id("bmw_fwb"), trigger="test")
    assert len(_events(sent, "failure")) == 1

    # Still broken on the next three cycles -- must NOT alert again.
    for _ in range(3):
        service.check_business(business_id("bmw_fwb"), trigger="test")
    assert len(_events(sent, "failure")) == 1


def test_failure_alert_names_the_business_and_the_cause(fresh_db, service, stub_backend, sent):
    stub_backend.fail_keys["mb_fwb"] = AccessBlocked("Google served a CAPTCHA.")

    service.check_business(business_id("mb_fwb"), trigger="test")

    alert = _events(sent, "failure")[0]
    result = alert["results"][0]
    assert result["business_name"] == "Mercedes-Benz of Fort Walton Beach"
    assert result["error_type"] == "AccessBlocked"
    assert "CAPTCHA" in result["error_message"]


# --------------------------------------------------------------- recovery
def test_recovery_alert_fires_when_it_starts_working_again(
    fresh_db, service, stub_backend, sent
):
    stub_backend.fail_keys["bmw_fwb"] = AccessBlocked("blocked")
    service.check_business(business_id("bmw_fwb"), trigger="test")
    assert len(_events(sent, "failure")) == 1

    del stub_backend.fail_keys["bmw_fwb"]
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    assert len(_events(sent, "recovered")) == 1


def test_no_recovery_alert_when_it_was_already_healthy(fresh_db, service, stub_backend, sent):
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]
    service.check_business(business_id("bmw_fwb"), trigger="test")
    service.check_business(business_id("bmw_fwb"), trigger="test")

    assert _events(sent, "recovered") == []


# --------------------------------------------------------------- safety
def test_alerting_failure_never_breaks_a_check(fresh_db, service, stub_backend, monkeypatch):
    """A broken mail server must not lose reviews."""
    def boom(*a, **k):
        raise RuntimeError("mail server on fire")

    monkeypatch.setattr("app.services.collection_service.notifier.send_event", boom)
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]

    result = service.check_business(business_id("bmw_fwb"), trigger="test")

    assert result["status"] == "success"
    assert result["new_reviews"] == 1


def test_one_dealership_alerting_does_not_affect_the_other(
    fresh_db, service, stub_backend, sent
):
    stub_backend.fail_keys["bmw_fwb"] = AccessBlocked("blocked")
    stub_backend.reviews_by_key["mb_fwb"] = [make_review(5)]

    service.run_cycle(trigger="test")

    assert len(_events(sent, "failure")) == 1
    assert len(_events(sent, "new_review")) == 1
    assert _events(sent, "failure")[0]["results"][0]["business_name"].startswith("BMW")
    assert _events(sent, "new_review")[0]["results"][0]["business_name"].startswith("Mercedes")
