"""Backend selection and the honesty guarantees around failure."""
from __future__ import annotations

from app.collector.base import AccessBlocked, BackendUnavailable, CollectorError, RawReview
from app.collector.google_maps import GoogleMapsBackend
from app.collector.registry import resolve_backend_order
from app.config import Settings


def test_backend_order_without_credentials_is_playwright_only():
    settings = Settings(COLLECTOR_BACKEND="auto", GOOGLE_CLIENT_ID="", GOOGLE_REFRESH_TOKEN="")
    assert resolve_backend_order(settings) == ["playwright"]


def test_backend_order_prefers_the_official_api_when_credentials_exist():
    settings = Settings(
        COLLECTOR_BACKEND="auto",
        GOOGLE_CLIENT_ID="id",
        GOOGLE_CLIENT_SECRET="secret",
        GOOGLE_REFRESH_TOKEN="token",
    )
    assert resolve_backend_order(settings) == ["gbp_api", "playwright"]


def test_explicit_backend_is_respected():
    assert resolve_backend_order(Settings(COLLECTOR_BACKEND="gbp_api")) == ["gbp_api"]
    assert resolve_backend_order(Settings(COLLECTOR_BACKEND="playwright")) == ["playwright"]
    assert resolve_backend_order(Settings(COLLECTOR_BACKEND="google_maps")) == ["playwright"]


def test_unknown_backend_falls_back_to_auto():
    assert resolve_backend_order(Settings(COLLECTOR_BACKEND="nonsense")) == ["playwright"]


def test_maps_backend_pins_the_exact_listing_via_place_id():
    class FakeBusiness:
        name = "BMW of Fort Walton Beach"
        place_id = "ChIJ0XFsWkI-kYgR1j9GfmxgynY"
        google_url = None

    url = GoogleMapsBackend.place_url(FakeBusiness())
    assert "place_id:ChIJ0XFsWkI-kYgR1j9GfmxgynY" in url
    assert "hl=en" in url and "gl=us" in url


def test_maps_backend_refuses_to_run_without_an_identifier():
    class FakeBusiness:
        name = "BMW of Fort Walton Beach"
        place_id = None
        google_url = None

    try:
        GoogleMapsBackend.place_url(FakeBusiness())
        raised = False
    except BackendUnavailable:
        raised = True
    assert raised


def test_access_blocked_is_retryable_and_unavailable_is_not():
    """The retry advice reported to the user must be accurate."""
    assert AccessBlocked("blocked").retryable is True
    assert BackendUnavailable("no credentials").retryable is False
    assert isinstance(AccessBlocked("x"), CollectorError)


def test_raw_review_requires_real_content():
    assert RawReview(rating=5).has_content() is True
    assert RawReview(review_text="Nice").has_content() is True
    assert RawReview().has_content() is False
    assert RawReview(reviewer_name="Someone").has_content() is False
