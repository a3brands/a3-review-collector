"""Tests 4-7 and 11: the A3 contract and API-key authentication."""
from __future__ import annotations

from app.database.database import session_scope
from app.database.models import Review
from tests.conftest import business_id, make_review


def _seed(service, stub_backend, key="bmw_fwb", count=3):
    stub_backend.reviews_by_key[key] = [make_review(i) for i in range(1, count + 1)]
    service.check_business(business_id(key), trigger="test")


# --------------------------------------------------------------- Test 4 + 5
def test_new_endpoint_returns_unprocessed_reviews(client, auth, service, stub_backend):
    """Test 4: GET /api/reviews/new returns the unprocessed queue.
       Test 5: A3 authenticates with a Bearer token and receives the reviews."""
    _seed(service, stub_backend, count=3)

    response = client.get("/api/reviews/new", headers=auth)

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    assert len(body["reviews"]) == 3

    review = body["reviews"][0]
    for field in (
        "business_name", "business_id", "place_id", "review_id", "reviewer_name",
        "reviewer_profile_url", "rating", "review_text", "review_date",
        "review_url", "detected_at", "processed", "processed_at",
    ):
        assert field in review, f"missing field {field}"

    assert review["business_name"] == "BMW of Fort Walton Beach"
    assert review["place_id"] == "ChIJ0XFsWkI-kYgR1j9GfmxgynY"
    assert review["processed"] is False


def test_new_endpoint_is_empty_before_any_collection(client, auth):
    response = client.get("/api/reviews/new", headers=auth)
    assert response.status_code == 200
    assert response.json() == {"count": 0, "total": 0, "reviews": []}


def test_new_endpoint_supports_filtering(client, auth, service, stub_backend):
    _seed(service, stub_backend, "bmw_fwb", 2)
    _seed(service, stub_backend, "mb_fwb", 3)

    assert client.get("/api/reviews/new", headers=auth).json()["count"] == 5
    assert client.get("/api/reviews/new?business=bmw_fwb", headers=auth).json()["count"] == 2
    assert client.get("/api/reviews/new?limit=1", headers=auth).json()["count"] == 1


# --------------------------------------------------------------- Test 6 + 7
def test_mark_processed_removes_review_from_the_queue(client, auth, service, stub_backend):
    """Test 6: mark a review processed.
       Test 7: it then disappears from /api/reviews/new."""
    _seed(service, stub_backend, count=3)

    target = client.get("/api/reviews/new", headers=auth).json()["reviews"][0]["review_id"]

    marked = client.post(f"/api/reviews/{target}/processed", headers=auth)
    assert marked.status_code == 200
    assert marked.json()["ok"] is True
    assert marked.json()["already_processed"] is False
    assert marked.json()["review"]["processed"] is True
    assert marked.json()["review"]["processed_at"] is not None

    remaining = client.get("/api/reviews/new", headers=auth).json()
    assert remaining["count"] == 2
    assert target not in [r["review_id"] for r in remaining["reviews"]]

    with session_scope() as session:
        row = session.query(Review).filter_by(review_id=target).one()
        assert row.processed is True
        assert row.processed_at is not None


def test_mark_processed_is_idempotent(client, auth, service, stub_backend):
    """A3 retrying its acknowledgement must not error."""
    _seed(service, stub_backend, count=1)
    target = client.get("/api/reviews/new", headers=auth).json()["reviews"][0]["review_id"]

    first = client.post(f"/api/reviews/{target}/processed", headers=auth)
    second = client.post(f"/api/reviews/{target}/processed", headers=auth)

    assert first.json()["already_processed"] is False
    assert second.status_code == 200
    assert second.json()["already_processed"] is True


def test_mark_processed_unknown_id_returns_404(client, auth):
    response = client.post("/api/reviews/does-not-exist/processed", headers=auth)
    assert response.status_code == 404


def test_review_can_be_returned_to_the_queue(client, auth, service, stub_backend):
    _seed(service, stub_backend, count=1)
    target = client.get("/api/reviews/new", headers=auth).json()["reviews"][0]["review_id"]

    client.post(f"/api/reviews/{target}/processed", headers=auth)
    assert client.get("/api/reviews/new", headers=auth).json()["count"] == 0

    client.post(f"/api/reviews/{target}/unprocessed", headers=auth)
    assert client.get("/api/reviews/new", headers=auth).json()["count"] == 1


def test_collector_never_resends_a_processed_review(client, auth, service, stub_backend):
    """The end-to-end guarantee: A3 must never receive the same review twice."""
    _seed(service, stub_backend, count=2)

    first_batch = client.get("/api/reviews/new", headers=auth).json()["reviews"]
    for review in first_batch:
        client.post(f"/api/reviews/{review['review_id']}/processed", headers=auth)

    # Collector runs again and re-sees the very same reviews on Google.
    service.check_business(business_id("bmw_fwb"), trigger="test")

    assert client.get("/api/reviews/new", headers=auth).json()["count"] == 0


# --------------------------------------------------------------- Test 11
def test_api_requires_authentication(client):
    """Test 11: verify API-key authentication."""
    assert client.get("/api/reviews/new").status_code == 401
    assert client.get("/api/reviews").status_code == 401
    assert client.get("/api/admin/status").status_code == 401
    assert client.post("/api/admin/check-now").status_code == 401


def test_api_rejects_a_wrong_key(client):
    response = client.get("/api/reviews/new", headers={"Authorization": "Bearer wrong-key"})
    assert response.status_code == 403


def test_api_rejects_a_malformed_authorization_header(client):
    for header in ("", "Basic abc", "Bearer", "token test-key-do-not-use-in-production"):
        response = client.get("/api/reviews/new", headers={"Authorization": header})
        assert response.status_code in (401, 403), header


def test_api_accepts_the_x_api_key_header(client):
    response = client.get(
        "/api/reviews/new", headers={"X-API-Key": "test-key-do-not-use-in-production"}
    )
    assert response.status_code == 200


def test_health_endpoint_is_public_and_leaks_nothing(client):
    """Requirement 11: /health must work without a key."""
    response = client.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] in ("ok", "degraded", "error")
    for field in ("last_check", "next_check", "scheduler_running", "database", "detail"):
        assert field in body

    text = response.text
    assert "test-key-do-not-use-in-production" not in text
    assert "ChIJ" not in text  # no place IDs, no review content


def test_admin_status_shape(client, auth, service, stub_backend):
    _seed(service, stub_backend, count=2)

    body = client.get("/api/admin/status", headers=auth).json()

    assert {"businesses", "totals", "scheduler", "backends", "config"} <= set(body)
    assert len(body["businesses"]) == 2
    names = {b["name"] for b in body["businesses"]}
    assert names == {"BMW of Fort Walton Beach", "Mercedes-Benz of Fort Walton Beach"}
    assert body["totals"]["total_reviews_collected"] == 2
    assert body["totals"]["unprocessed_reviews"] == 2
    assert body["config"]["check_interval_minutes"] == 15


def test_health_reports_degraded_when_collection_is_failing(client, service, stub_backend):
    """Requirement 20: never claim the system is working when it is not."""
    from app.collector.base import AccessBlocked
    from app.scheduler.scheduler import get_scheduler, reset_scheduler

    reset_scheduler()
    get_scheduler().start()
    try:
        stub_backend.fail_keys["bmw_fwb"] = AccessBlocked("Google served the limited view.")
        stub_backend.reviews_by_key["mb_fwb"] = [make_review(1)]
        service.run_cycle(trigger="test")

        body = client.get("/health").json()
        assert body["status"] == "degraded"
        assert "BMW of Fort Walton Beach" in body["detail"]
        assert body["businesses_configured"] == 2
        assert body["businesses_healthy"] == 1
    finally:
        reset_scheduler()


def test_health_reports_error_when_the_scheduler_is_stopped(client):
    from app.scheduler.scheduler import get_scheduler, reset_scheduler

    reset_scheduler()
    get_scheduler().stop()
    body = client.get("/health").json()
    assert body["status"] == "error"
    assert "Scheduler is not running" in body["detail"]
    reset_scheduler()


def test_all_timestamps_are_utc_with_a_z_suffix(client, auth, service, stub_backend):
    """A3 must never have to guess a timezone."""
    _seed(service, stub_backend, count=1)

    review = client.get("/api/reviews/new", headers=auth).json()["reviews"][0]
    for field in ("detected_at", "review_date"):
        assert review[field].endswith("Z"), f"{field} is not UTC-suffixed: {review[field]}"

    marked = client.post(f"/api/reviews/{review['review_id']}/processed", headers=auth).json()
    assert marked["review"]["processed_at"].endswith("Z")

    checks = client.get("/api/admin/checks", headers=auth).json()["checks"]
    assert checks and checks[0]["started_at"].endswith("Z")


def test_urgent_sort_surfaces_the_worst_review_across_the_whole_queue(
    client, auth, service, stub_backend
):
    """The angriest review must reach page 1 even when the queue is long.

    Sorting only the rows already on screen would hide a 1-star review behind
    a page of 5-star ones -- which is exactly what the dashboard used to do.
    """
    import datetime as dt

    reviews = [make_review(i, rating=5, review_date=dt.datetime(2026, 8, i % 28 + 1))
               for i in range(1, 15)]
    # Collection reads the newest N, so the 1-star sits among them rather than
    # beyond the fetch window.
    reviews.insert(3, make_review(99, rating=1, review_date=dt.datetime(2025, 8, 20)))
    stub_backend.reviews_by_key["bmw_fwb"] = reviews
    service.check_business(business_id("bmw_fwb"), trigger="test")

    body = client.get("/api/reviews/new?limit=3&sort=urgent", headers=auth).json()

    assert body["reviews"][0]["rating"] == 1
    assert body["reviews"][0]["reviewer_name"] == "Reviewer 99"


def test_sort_options_change_the_order(client, auth, service, stub_backend):
    import datetime as dt

    stub_backend.reviews_by_key["bmw_fwb"] = [
        make_review(1, rating=5, review_date=dt.datetime(2024, 1, 1)),
        make_review(2, rating=3, review_date=dt.datetime(2026, 8, 1)),
    ]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    oldest = client.get("/api/reviews?limit=5&sort=oldest", headers=auth).json()["reviews"]
    newest = client.get("/api/reviews?limit=5&sort=newest", headers=auth).json()["reviews"]
    urgent = client.get("/api/reviews?limit=5&sort=urgent", headers=auth).json()["reviews"]

    assert oldest[0]["review_date"].startswith("2024")
    assert newest[0]["review_date"].startswith("2026")
    assert urgent[0]["rating"] == 3


def test_a3_default_order_is_still_arrival_order(client, auth, service, stub_backend):
    """A3 polls without a sort param and must keep getting oldest-detected first."""
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1), make_review(2)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    body = client.get("/api/reviews/new", headers=auth).json()
    assert [r["reviewer_name"] for r in body["reviews"]] == ["Reviewer 1", "Reviewer 2"]


def test_invalid_sort_is_rejected(client, auth):
    assert client.get("/api/reviews/new?sort=bogus", headers=auth).status_code == 422


def test_queue_summary_reports_what_needs_attention(client, auth, service, stub_backend):
    """The dashboard leads with this, so it must be accurate."""
    import datetime as dt

    stub_backend.reviews_by_key["bmw_fwb"] = [
        make_review(1, rating=5),
        make_review(2, rating=1, review_date=dt.datetime(2025, 8, 20)),
    ]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    queue = client.get("/api/admin/status", headers=auth).json()["queue"]
    assert queue["waiting"] == 2
    assert queue["negative"] == 1
    assert queue["worst_rating"] == 1
    assert queue["oldest_posted_days"] > 300


def test_new_endpoint_reports_the_whole_queue_not_just_the_page(
    client, auth, service, stub_backend
):
    """The dashboard heading showed '(10)' while 18 were waiting."""
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(i) for i in range(1, 8)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    body = client.get("/api/reviews/new?limit=3", headers=auth).json()
    assert body["count"] == 3      # this page
    assert body["total"] == 7      # the whole queue


def test_newest_and_oldest_are_not_the_same_order(client, auth, service, stub_backend):
    """They returned an identical first review because 'oldest' was really
    'order detected', not 'oldest review'."""
    import datetime as dt

    stub_backend.reviews_by_key["bmw_fwb"] = [
        make_review(1, rating=5, review_date=dt.datetime(2024, 3, 1)),
        make_review(2, rating=5, review_date=dt.datetime(2026, 7, 1)),
    ]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    newest = client.get("/api/reviews/new?limit=1&sort=newest", headers=auth).json()["reviews"][0]
    oldest = client.get("/api/reviews/new?limit=1&sort=oldest", headers=auth).json()["reviews"][0]

    assert newest["review_date"].startswith("2026")
    assert oldest["review_date"].startswith("2024")
    assert newest["review_id"] != oldest["review_id"]


def test_arrival_order_is_the_untouched_default_for_a3(client, auth, service, stub_backend):
    import datetime as dt

    stub_backend.reviews_by_key["bmw_fwb"] = [
        make_review(1, review_date=dt.datetime(2026, 7, 1)),
        make_review(2, review_date=dt.datetime(2024, 3, 1)),
    ]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    default = client.get("/api/reviews/new", headers=auth).json()["reviews"]
    arrival = client.get("/api/reviews/new?sort=arrival", headers=auth).json()["reviews"]
    assert [r["review_id"] for r in default] == [r["review_id"] for r in arrival]
    assert default[0]["reviewer_name"] == "Reviewer 1"
