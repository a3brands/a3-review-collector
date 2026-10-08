"""Pushing reviews into the Google Reviews Manager (the Railway app).

The fake manager here re-implements the verification in that app's
src/collector-ingest.js, so a signing mistake fails here rather than silently
in production.
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
from app.services import grm_sync

SECRET = "g" * 40


class FakeManager(BaseHTTPRequestHandler):
    received = []
    force_status = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        # Read the body before any reply. Closing a socket with unread request
        # data makes Windows reset the connection (WinError 10053), so the
        # client never sees the forced status.
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        if FakeManager.force_status:
            return self._json(FakeManager.force_status, {"ok": False, "error": "forced"})

        body = json.loads(raw or b"{}")
        payload = body.get("payload")
        if not isinstance(payload, str):
            return self._json(400, {"ok": False, "error": "bad_envelope"})

        ts = self.headers.get("x-a3-timestamp")
        sig = (self.headers.get("x-a3-signature") or "").replace("sha256=", "").lower()
        if not ts or not sig:
            return self._json(401, {"ok": False, "error": "missing_signature_headers"})
        if abs(int(time.time()) - int(ts)) > 300:
            return self._json(401, {"ok": False, "error": "stale_timestamp"})
        expected = hmac.new(SECRET.encode(), f"{int(ts)}.{payload}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return self._json(401, {"ok": False, "error": "bad_signature"})

        slug = self.path.split("/api/ingest/")[-1].split("/")[0]
        if slug not in ("bmw-fwb", "mb-fwb"):
            return self._json(404, {"ok": False, "error": f"unknown dealer '{slug}'"})

        parsed = json.loads(payload)
        FakeManager.received.append({"slug": slug, "body": parsed})
        self._json(200, {"ok": True, "dealer": slug, "source": "google",
                         "reviews_written": len(parsed["reviews"])})

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def manager():
    FakeManager.received = []
    FakeManager.force_status = None
    server = HTTPServer(("127.0.0.1", 0), FakeManager)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.fixture
def grm_settings(manager):
    return Settings(
        GRM_ENABLED="true",
        GRM_URL=f"http://127.0.0.1:{manager.server_address[1]}",
        GRM_INGEST_SECRET=SECRET,
        GRM_MAX_RETRIES="0",
    )


def review(**over):
    base = {
        "reviewer_name": "Jane Doe",
        "rating": 5,
        "review_text": "Great service.",
        "review_date": "2026-08-01T10:00:00Z",
        "review_url": "https://maps.google.com/x",
        "google_review_id": "ChZDSUhN",
        "owner_replied": False,
        "owner_reply": None,
    }
    base.update(over)
    return base


def test_manager_accepts_our_signature(manager, grm_settings):
    out = grm_sync.push_dealer("bmw-fwb", [review()], {"placeId": "ChIJ..."}, settings=grm_settings)
    assert out["ok"] is True
    assert out["reviews_written"] == 1


def test_fields_match_what_review_store_normalises(manager, grm_settings):
    grm_sync.push_dealer("bmw-fwb", [review()], {}, settings=grm_settings)

    item = FakeManager.received[0]["body"]["reviews"][0]
    assert item["stars"] == 5                     # -> starRating
    assert item["name"] == "Jane Doe"             # -> reviewer.displayName
    assert item["text"] == "Great service."       # -> comment
    assert item["publishedAtDate"]                # -> createTime
    assert item["reviewId"] == "ChZDSUhN"         # -> platformReviewId, used by a3-sync


def test_a_known_reply_is_flagged_even_without_its_text(manager, grm_settings):
    """Otherwise the dashboard reports 0% response rate for a dealer who
    answers nearly everything."""
    grm_sync.push_dealer("bmw-fwb", [review(owner_replied=True)], {}, settings=grm_settings)

    item = FakeManager.received[0]["body"]["reviews"][0]
    assert item["hasOwnerReply"] is True
    assert item["responseFromOwnerText"] is None   # never invented


def test_business_info_is_carried_through(manager, grm_settings):
    grm_sync.push_dealer("bmw-fwb", [review()],
                         {"placeId": "ChIJ0XFsWkI", "totalScore": 4.5}, settings=grm_settings)
    body = FakeManager.received[0]["body"]
    assert body["business_info"]["placeId"] == "ChIJ0XFsWkI"
    assert body["review_summary"]["pulled_at"]


def test_both_dealer_slugs_are_accepted(manager, grm_settings):
    for slug in ("bmw-fwb", "mb-fwb"):
        assert grm_sync.push_dealer(slug, [review()], {}, settings=grm_settings)["ok"] is True
    assert [r["slug"] for r in FakeManager.received] == ["bmw-fwb", "mb-fwb"]


def test_unknown_dealer_is_explained(manager, grm_settings):
    with pytest.raises(grm_sync.GRMSyncError) as exc:
        grm_sync.push_dealer("ford-fwb", [review()], {}, settings=grm_settings)
    assert "does not know dealer" in str(exc.value)


def test_missing_configuration_is_caught_before_sending(manager):
    with pytest.raises(grm_sync.GRMNotConfigured):
        grm_sync.push_dealer("bmw-fwb", [review()], {}, settings=Settings(GRM_URL=""))


def test_short_secret_is_rejected(manager):
    with pytest.raises(grm_sync.GRMNotConfigured) as exc:
        grm_sync.push_dealer("bmw-fwb", [review()], {},
                             settings=Settings(GRM_URL="http://x", GRM_INGEST_SECRET="short"))
    assert "at least 32 characters" in str(exc.value)


def test_unconfigured_manager_is_explained(manager, grm_settings):
    FakeManager.force_status = 503
    with pytest.raises(grm_sync.GRMSyncError) as exc:
        grm_sync.push_dealer("bmw-fwb", [review()], {}, settings=grm_settings)
    assert "ingest_not_configured" in str(exc.value)


def test_bad_secret_is_explained(manager, grm_settings):
    wrong = grm_settings.model_copy(update={"grm_ingest_secret": "w" * 40})
    with pytest.raises(grm_sync.GRMSyncError) as exc:
        grm_sync.push_dealer("bmw-fwb", [review()], {}, settings=wrong)
    assert "must match" in str(exc.value)


def test_dry_run_sends_nothing(manager, grm_settings):
    out = grm_sync.push_dealer("bmw-fwb", [review()], {}, settings=grm_settings, dry_run=True)
    assert out["dry_run"] is True
    assert FakeManager.received == []


# --------------------------------------------------- automatic push per cycle
def test_every_check_pushes_to_the_manager(fresh_db, service, stub_backend, manager, monkeypatch):
    """It must run on the 15-minute cycle, not only when somebody clicks."""
    from tests.conftest import business_id, make_review

    monkeypatch.setattr(service.settings, "grm_enabled", True)
    monkeypatch.setattr(service.settings, "grm_url",
                        f"http://127.0.0.1:{manager.server_address[1]}")
    monkeypatch.setattr(service.settings, "grm_ingest_secret", SECRET)

    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1), make_review(2)]
    stub_backend.reviews_by_key["mb_fwb"] = [make_review(3)]

    service.run_cycle(trigger="scheduled")

    pushed = {r["slug"]: len(r["body"]["reviews"]) for r in FakeManager.received}
    assert pushed == {"bmw-fwb": 2, "mb-fwb": 1}


def test_a_dead_manager_never_breaks_collection(fresh_db, service, stub_backend, monkeypatch):
    """Reviews must still be collected and stored if the manager is down."""
    from app.database.database import session_scope
    from app.database.models import Review
    from tests.conftest import business_id, make_review

    monkeypatch.setattr(service.settings, "grm_enabled", True)
    monkeypatch.setattr(service.settings, "grm_url", "http://127.0.0.1:1")   # nothing listening
    monkeypatch.setattr(service.settings, "grm_ingest_secret", SECRET)
    monkeypatch.setattr(service.settings, "grm_max_retries", 0)

    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]
    cycle = service.run_cycle(trigger="scheduled")

    assert all(r["status"] == "success" for r in cycle["results"])
    with session_scope() as session:
        assert session.query(Review).count() == 1


def test_sync_stays_off_until_enabled(fresh_db, service, stub_backend, manager):
    from tests.conftest import make_review

    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]
    service.run_cycle(trigger="scheduled")
    assert FakeManager.received == []
