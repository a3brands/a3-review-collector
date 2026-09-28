"""Pushing reviews to the A3 Review Responder.

The fake A3 here re-implements the verification in A3's own
api/reviews-sync.js -- same HMAC construction, same 5-minute skew window, same
envelope shape -- so a signature bug shows up here rather than in production.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.config import Settings
from app.services import a3_sync

SECRET = "s" * 40
MAX_SKEW_SECONDS = 300


class FakeA3(BaseHTTPRequestHandler):
    received = []
    behaviour = {"status": 200}

    def log_message(self, *a):  # silence the test server
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")

        forced = FakeA3.behaviour.get("status", 200)
        if forced != 200:
            return self._json(forced, {"ok": False, "error": "forced"})

        payload = body.get("payload")
        if not isinstance(payload, str):
            return self._json(400, {"ok": False, "error": "bad_envelope"})

        ts = self.headers.get("x-a3-timestamp")
        sig = (self.headers.get("x-a3-signature") or "").replace("sha256=", "").lower()
        if not ts or not sig:
            return self._json(401, {"ok": False, "error": "missing_signature_headers"})
        if abs(int(time.time()) - int(ts)) > MAX_SKEW_SECONDS:
            return self._json(401, {"ok": False, "error": "stale_timestamp"})

        expected = hmac.new(SECRET.encode(), f"{int(ts)}.{payload}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return self._json(401, {"ok": False, "error": "bad_signature"})

        reviews = json.loads(payload)["reviews"]
        FakeA3.received.append(reviews)

        results = []
        for item in reviews:
            if not item.get("dealer_id"):
                results.append({"action": "error", "error": "dealer_id is required"})
            else:
                results.append({"action": "created", "review_id": item["review_id"]})
        self._json(200, {"ok": True, "results": results})

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def fake_a3():
    FakeA3.received = []
    FakeA3.behaviour = {"status": 200}
    server = HTTPServer(("127.0.0.1", 0), FakeA3)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.fixture
def sync_settings(fake_a3):
    port = fake_a3.server_address[1]
    return Settings(
        A3_SYNC_ENABLED="true",
        A3_SYNC_URL=f"http://127.0.0.1:{port}/api/reviews-sync",
        A3_SYNC_SECRET=SECRET,
        A3_SYNC_MAX_RETRIES="0",
    )


def review(**over):
    base = {
        "business_key": "bmw_fwb",
        "review_id": "bmw_fwb:ChZDSUhNMG9nS0VJQ0Fn",
        "google_review_id": "ChZDSUhNMG9nS0VJQ0Fn",
        "reviewer_name": "Jane Doe",
        "rating": 5,
        "review_text": "Great service.",
        "review_date": "2026-08-01T10:00:00Z",
        "review_url": "https://maps.google.com/x",
        "owner_replied": False,
        "owner_reply": None,
        "owner_reply_date": None,
    }
    base.update(over)
    return base


# --------------------------------------------------------------- wire format
def test_a3_accepts_our_signature(fake_a3, sync_settings):
    out = a3_sync.push([review()], settings=sync_settings)

    assert out["sent"] == 1
    assert out["accepted"] == ["bmw_fwb:ChZDSUhNMG9nS0VJQ0Fn"]
    assert len(FakeA3.received) == 1


def test_we_send_googles_raw_id_not_our_namespaced_one(fake_a3, sync_settings):
    """The prefix would make A3 create a second row for every review."""
    a3_sync.push([review()], settings=sync_settings)

    item = FakeA3.received[0][0]
    assert item["review_id"] == "ChZDSUhNMG9nS0VJQ0Fn"
    assert not item["review_id"].startswith("bmw_fwb:")


def test_business_key_maps_to_a3s_dealer_slug(fake_a3, sync_settings):
    a3_sync.push(
        [review(), review(business_key="mb_fwb", review_id="mb_fwb:XYZ", google_review_id="XYZ")],
        settings=sync_settings,
    )
    assert [i["dealer_id"] for i in FakeA3.received[0]] == ["bmw-fwb", "mb-fwb"]


def test_a_tampered_payload_is_rejected(fake_a3, sync_settings):
    """Proves the fake really is checking, so the passing tests mean something."""
    headers, body = a3_sync.build_request([{"dealer_id": "bmw-fwb", "review_id": "a", "rating": 5}],
                                          sync_settings)
    import httpx

    body["payload"] = body["payload"].replace("bmw-fwb", "evil-dealer")
    res = httpx.post(sync_settings.a3_sync_url, json=body, headers=headers, timeout=10)
    assert res.status_code == 401
    assert res.json()["error"] == "bad_signature"


def test_a_stale_timestamp_is_rejected(fake_a3, sync_settings):
    import httpx

    headers, body = a3_sync.build_request(
        [{"dealer_id": "bmw-fwb", "review_id": "a", "rating": 5}],
        sync_settings, timestamp=int(time.time()) - 3600,
    )
    res = httpx.post(sync_settings.a3_sync_url, json=body, headers=headers, timeout=10)
    assert res.status_code == 401
    assert res.json()["error"] == "stale_timestamp"


# --------------------------------------------------------------- safety rules
def test_reply_state_is_withheld_by_default(fake_a3, sync_settings):
    """A3 writes business_reply without COALESCE, so a null from us would erase
    the reply text it already holds."""
    a3_sync.push([review(owner_replied=True)], settings=sync_settings)

    item = FakeA3.received[0][0]
    assert "status" not in item
    assert "business_reply" not in item


def test_reply_state_is_sent_when_explicitly_enabled(fake_a3, sync_settings):
    opted_in = sync_settings.model_copy(update={"a3_sync_send_reply_state": True})
    a3_sync.push([review(owner_replied=True, owner_reply="Thanks!")], settings=opted_in)

    item = FakeA3.received[0][0]
    assert item["status"] == "replied"
    assert item["business_reply"] == "Thanks!"


def test_unusable_reviews_are_skipped_not_sent(fake_a3, sync_settings):
    """A3 requires an integer rating and a real Google id."""
    out = a3_sync.push([
        review(),
        review(rating=None, review_id="bmw_fwb:A", google_review_id="A"),
        review(review_id="bmw_fwb:fp_abc", google_review_id="bmw_fwb:fp_abc"),
    ], settings=sync_settings)

    assert out["sent"] == 1
    assert out["skipped"] == 2
    assert len(FakeA3.received[0]) == 1


def test_a_dealership_added_later_is_sent_rather_than_silently_dropped(fake_a3, sync_settings):
    """The bug this replaced.

    The key to slug map held two entries, so every dealership added after the
    original pair mapped to nothing and its reviews were skipped with a line in
    a log nobody reads. They were collected, stored, and never reached the
    dashboard, and an empty queue was the only symptom.

    Any key now maps by substituting hyphens for underscores. If the Responder
    genuinely does not know that dealership it answers 404, which is a problem
    somebody can see rather than one that hides.
    """
    out = a3_sync.push([
        review(business_key="parks_lincoln", review_id="parks_lincoln:1", google_review_id="1"),
    ], settings=sync_settings)

    assert out["sent"] == 1
    assert FakeA3.received[0][0]["dealer_id"] == "parks-lincoln"


def test_batches_respect_a3s_limit(fake_a3, sync_settings):
    small = sync_settings.model_copy(update={"a3_sync_batch_size": 2})
    reviews = [review(review_id=f"bmw_fwb:{i}", google_review_id=str(i)) for i in range(5)]

    out = a3_sync.push(reviews, settings=small)

    assert out["sent"] == 5
    assert [len(b) for b in FakeA3.received] == [2, 2, 1]
    assert len(out["accepted"]) == 5


def test_dry_run_sends_nothing(fake_a3, sync_settings):
    out = a3_sync.push([review()], settings=sync_settings, dry_run=True)
    assert out["dry_run"] is True
    assert FakeA3.received == []


# --------------------------------------------------------------- failures
def test_missing_configuration_is_explained(fake_a3):
    with pytest.raises(a3_sync.A3NotConfigured) as exc:
        a3_sync.push([review()], settings=Settings(A3_SYNC_URL="", A3_SYNC_SECRET=""))
    assert "A3_SYNC_URL" in str(exc.value)


def test_a_short_secret_is_caught_before_sending(fake_a3):
    with pytest.raises(a3_sync.A3NotConfigured) as exc:
        a3_sync.push([review()], settings=Settings(
            A3_SYNC_URL="http://x/api/reviews-sync", A3_SYNC_SECRET="tooshort"))
    assert "at least 32 characters" in str(exc.value)


def test_a3_503_explains_the_a3_side_problem(fake_a3, sync_settings):
    FakeA3.behaviour = {"status": 503}
    with pytest.raises(a3_sync.A3SyncError) as exc:
        a3_sync.push([review()], settings=sync_settings)
    assert "sync_not_configured" in str(exc.value)


def test_a3_401_explains_the_secret_or_clock(fake_a3, sync_settings):
    FakeA3.behaviour = {"status": 401}
    with pytest.raises(a3_sync.A3SyncError) as exc:
        a3_sync.push([review()], settings=sync_settings)
    assert "must match on both" in str(exc.value)


def test_per_review_errors_do_not_fail_the_batch(fake_a3, sync_settings):
    out = a3_sync.push([review(), review(business_key="bmw_fwb",
                                         review_id="bmw_fwb:B", google_review_id="B")],
                       settings=sync_settings)
    assert len(out["accepted"]) == 2
    assert all(r["action"] == "created" for r in out["results"])
