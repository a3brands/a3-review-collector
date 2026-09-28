"""The daily "still alive" summary.

Its whole value is that it arrives. Every other alert here fires on an event, so
without it a quiet day and a dead process are the same empty inbox. That makes
two things worth testing hard: that it goes exactly once a day, and that a
machine which was asleep at the appointed hour still sends it on waking.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.config import get_settings
from app.services import heartbeat
from tests.conftest import business_id, make_review


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat-state.json"
    monkeypatch.setattr(heartbeat, "STATE_FILE", path)
    return path


@pytest.fixture
def on(monkeypatch):
    s = get_settings().model_copy(update={"notify_on_heartbeat": True, "heartbeat_hour": 8})
    return s


def test_not_due_before_the_hour(state_file, on):
    assert heartbeat.is_due(dt.datetime(2026, 9, 10, 7, 59), on) is False


def test_due_once_the_hour_has_passed(state_file, on):
    assert heartbeat.is_due(dt.datetime(2026, 9, 10, 8, 0), on) is True


def test_a_machine_woken_late_still_sends_it(state_file, on):
    # The trap this exists to avoid: a job pinned to 08:00 on a laptop that was
    # off at 08:00 simply never runs. Waking at 14:00 must still send.
    assert heartbeat.is_due(dt.datetime(2026, 9, 10, 14, 30), on) is True


def test_only_one_a_day(state_file, on):
    heartbeat.mark_sent("2026-09-10")
    assert heartbeat.is_due(dt.datetime(2026, 9, 10, 9, 0), on) is False
    # ...and it comes back the next day.
    assert heartbeat.is_due(dt.datetime(2026, 9, 11, 8, 30), on) is True


def test_disabled_is_never_due(state_file):
    off = get_settings().model_copy(update={"notify_on_heartbeat": False, "heartbeat_hour": 0})
    assert heartbeat.is_due(dt.datetime(2026, 9, 10, 23, 0), off) is False


def test_an_unreadable_marker_does_not_stop_it(state_file, on):
    state_file.write_text("{ truncated", "utf-8")
    # Losing the marker costs at most one duplicate; crashing costs the alert.
    assert heartbeat.is_due(dt.datetime(2026, 9, 10, 9, 0), on) is True


def test_the_marker_survives_a_write(state_file, on):
    heartbeat.mark_sent("2026-09-10")
    assert heartbeat.last_sent_day() == "2026-09-10"
    assert not list(state_file.parent.glob("*.tmp"))


def test_summarise_reports_every_active_dealership(fresh_db, service, stub_backend):
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1), make_review(2)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    summary = heartbeat.summarise()
    names = [r["business_name"] for r in summary["results"]]

    assert summary["window_hours"] == 24
    # Every active dealership appears, including the ones with nothing to say --
    # a heartbeat that silently omits a dealership is how one stops being
    # noticed at all.
    assert names == sorted(names)
    assert len(names) == 2


def test_counts_are_attributed_to_the_right_dealership(fresh_db, service, stub_backend):
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1), make_review(2)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    summary = heartbeat.summarise()
    bmw = next(r for r in summary["results"] if "BMW" in r["business_name"])
    other = next(r for r in summary["results"] if "BMW" not in r["business_name"])

    assert bmw["new_reviews"] == 2
    assert bmw["checks"] == 1
    assert other["new_reviews"] == 0
    assert other["checks"] == 0


def test_a_dealership_that_collected_is_not_reported_as_an_outage(fresh_db, service, stub_backend):
    # 'failed' means every check in the window failed. One failure among
    # successes is a retry, and reporting it as an outage would have the
    # heartbeat cry wolf daily until everyone ignored it.
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    summary = heartbeat.summarise()
    bmw = next(r for r in summary["results"] if "BMW" in r["business_name"])

    assert bmw["status"] == "success"
    assert bmw["error_type"] is None


def test_a_dealership_with_no_checks_at_all_is_not_called_failed(fresh_db, service, stub_backend):
    # Nothing ran for it, which is not the same as it failing. Calling that an
    # outage would fire an alert every time a dealership was newly added.
    summary = heartbeat.summarise()
    for row in summary["results"]:
        assert row["status"] == "success"
        assert row["checks"] == 0
