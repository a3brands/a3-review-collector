"""A review is emailed exactly once.

Before this, every Refresh re-sent the whole unprocessed queue -- including a
review posted in 2025 that had already been reported many times over.
"""
from __future__ import annotations

import datetime as dt

from app.database.database import session_scope
from app.database.models import Review
from app.services import stats
from tests.conftest import business_id, make_review


def _collect(service, stub_backend, *reviews, key="bmw_fwb"):
    stub_backend.reviews_by_key[key] = list(reviews)
    return service.check_business(business_id(key), trigger="test")


def test_a_new_review_is_unnotified_until_it_is_emailed(fresh_db, service, stub_backend):
    _collect(service, stub_backend, make_review(1))

    pending = stats.unnotified_reviews()
    assert len(pending) == 1

    stats.mark_notified([pending[0]["review_id"]])
    assert stats.unnotified_reviews() == []


def test_marking_is_idempotent(fresh_db, service, stub_backend):
    _collect(service, stub_backend, make_review(1))
    rid = stats.unnotified_reviews()[0]["review_id"]

    assert stats.mark_notified([rid]) == 1
    assert stats.mark_notified([rid]) == 0        # already stamped, not re-stamped


def test_only_the_genuinely_new_one_is_reported_next_time(fresh_db, service, stub_backend):
    _collect(service, stub_backend, make_review(1), make_review(2))
    stats.mark_notified([r["review_id"] for r in stats.unnotified_reviews()])

    _collect(service, stub_backend, make_review(3), make_review(1), make_review(2))

    pending = stats.unnotified_reviews()
    assert [r["reviewer_name"] for r in pending] == ["Reviewer 3"]


def test_old_reviews_never_re_enter_the_email(fresh_db, service, stub_backend):
    """A review that has waited a year has already been emailed; it must not
    come back every time someone clicks Refresh."""
    _collect(service, stub_backend, make_review(1, review_date=dt.datetime(2025, 8, 20)))
    stats.mark_notified([r["review_id"] for r in stats.unnotified_reviews()])

    # It is still unprocessed and still in the queue...
    with session_scope() as session:
        assert session.query(Review).filter_by(processed=False).count() == 1
    # ...but it is not "new" any more.
    assert stats.unnotified_reviews() == []


def test_a_stale_detection_falls_outside_the_window(fresh_db, service, stub_backend):
    """A big historical backfill must not arrive as a wall of 'new' mail."""
    _collect(service, stub_backend, make_review(1))
    with session_scope() as session:
        row = session.query(Review).one()
        row.detected_at = row.detected_at - dt.timedelta(hours=48)

    assert stats.unnotified_reviews(max_age_hours=24) == []
    assert len(stats.unnotified_reviews(max_age_hours=72)) == 1


def test_worst_rating_is_listed_first(fresh_db, service, stub_backend):
    _collect(service, stub_backend, make_review(1, rating=5), make_review(2, rating=1))
    assert stats.unnotified_reviews()[0]["rating"] == 1


# --------------------------------------------------- through the API
def test_refresh_sends_nothing_when_there_is_nothing_new(client, auth, service, stub_backend):
    _collect(service, stub_backend, make_review(1))
    stats.mark_notified([r["review_id"] for r in stats.unnotified_reviews()])

    body = client.post("/api/admin/notify", json={"event": "refresh"}, headers=auth).json()

    assert body["sent"] is False
    assert body["new_reviews"] == 0
    assert "no newly detected" in body["reason"]


def test_refresh_reports_only_the_new_ones(client, auth, service, stub_backend, monkeypatch):
    captured = {}

    def fake_send(event, **kwargs):
        # Only the Refresh path delivers here. The collector's own new-review
        # alert shares this module, and in production it would ALSO mark the
        # reviews notified -- which is why Refresh then correctly has nothing
        # new to say. This test isolates the Refresh path.
        if event != "refresh":
            return {"sent": False, "reason": "new_review alerts off for this test"}
        captured["reviews"] = kwargs.get("reviews")
        cb = kwargs.get("on_delivered")
        if cb:
            cb()
        return {"sent": True, "subject": "x"}

    monkeypatch.setattr("app.api.admin.notifier.send_event", fake_send)

    _collect(service, stub_backend, make_review(1), make_review(2))
    first = client.post("/api/admin/notify", json={"event": "refresh"}, headers=auth).json()
    assert first["new_reviews"] == 2

    # Clicking Refresh again must not resend them.
    second = client.post("/api/admin/notify", json={"event": "refresh"}, headers=auth).json()
    assert second["sent"] is False
    assert second["new_reviews"] == 0

    _collect(service, stub_backend, make_review(9), make_review(1), make_review(2))
    third = client.post("/api/admin/notify", json={"event": "refresh"}, headers=auth).json()
    assert third["new_reviews"] == 1
    assert [r["reviewer_name"] for r in captured["reviews"]] == ["Reviewer 9"]


def test_a_failed_send_leaves_them_still_unnotified(client, auth, service, stub_backend, monkeypatch):
    """If the mail never went out, the review must be reported next time."""
    monkeypatch.setattr(
        "app.api.admin.notifier.send_event",
        lambda event, **kw: {"sent": False, "reason": "smtp down"},
    )
    _collect(service, stub_backend, make_review(1))

    client.post("/api/admin/notify", json={"event": "refresh"}, headers=auth)

    assert len(stats.unnotified_reviews()) == 1


def test_new_review_alert_and_refresh_never_both_report_the_same_review(
    client, auth, service, stub_backend, monkeypatch
):
    """The two paths share one 'notified' stamp, so a review is emailed once
    in total -- not once by the alert and again by the next Refresh."""
    sent = []

    def fake_send(event, **kwargs):
        sent.append((event, [r["reviewer_name"] for r in (kwargs.get("reviews") or [])]))
        cb = kwargs.get("on_delivered")
        if cb:
            cb()
        return {"sent": True, "subject": "x"}

    monkeypatch.setattr("app.services.notifier.send_event", fake_send)
    monkeypatch.setattr("app.services.collection_service.notifier.send_event", fake_send)
    monkeypatch.setattr("app.api.admin.notifier.send_event", fake_send)

    _collect(service, stub_backend, make_review(1))
    assert ("new_review", ["Reviewer 1"]) in sent

    body = client.post("/api/admin/notify", json={"event": "refresh"}, headers=auth).json()
    assert body["sent"] is False          # the alert already covered it
    assert body["new_reviews"] == 0
