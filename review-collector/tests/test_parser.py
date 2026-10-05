"""Parsing layer -- the part most likely to need updating when Google changes."""
from __future__ import annotations

import datetime as dt

import pytest

from app.collector.parser import (
    clean_name,
    clean_review_text,
    parse_absolute_date,
    parse_gbp_review,
    parse_maps_card,
    parse_relative_date,
    parse_review_date,
    parse_star_rating,
)

NOW = dt.datetime(2026, 8, 19, 12, 0, 0)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("ONE", 1), ("THREE", 3), ("FIVE", 5),
        ("STAR_RATING_UNSPECIFIED", None),
        ("5 stars", 5), ("1 star", 1), ("4.0 stars", 4),
        ("Rated 2.0 out of 5, 2 stars", 2),
        (5, 5), (4.6, 5), (3.2, 3),
        (0, None), (7, None), ("", None), (None, None), ("nonsense", None),
    ],
)
def test_parse_star_rating(value, expected):
    assert parse_star_rating(value) == expected


def test_parse_absolute_date_handles_google_rfc3339():
    assert parse_absolute_date("2026-08-19T14:15:00Z") == dt.datetime(2026, 8, 19, 14, 15)
    # Google sometimes emits 9-digit fractional seconds, which Python rejects.
    assert parse_absolute_date("2026-08-19T14:15:00.123456789Z").replace(microsecond=0) == dt.datetime(
        2026, 8, 19, 14, 15
    )
    assert parse_absolute_date("") is None
    assert parse_absolute_date("last Tuesday") is None


@pytest.mark.parametrize(
    "text,days",
    [("a day ago", 1), ("2 days ago", 2), ("a week ago", 7),
     ("3 weeks ago", 21), ("a month ago", 30.44), ("a year ago", 365.25)],
)
def test_parse_relative_date(text, days):
    parsed, approximate = parse_relative_date(text, now=NOW)
    assert approximate is True
    assert abs((NOW - parsed).total_seconds() - days * 86400) < 60


def test_relative_dates_are_flagged_approximate_absolute_ones_are_not():
    _, approx = parse_review_date("2 weeks ago", now=NOW)
    assert approx is True
    _, exact = parse_review_date("2026-08-05T09:00:00Z", now=NOW)
    assert exact is False


def test_unparseable_date_returns_none_rather_than_guessing():
    parsed, approximate = parse_review_date("sometime recently", now=NOW)
    assert parsed is None and approximate is False


def test_clean_review_text():
    assert clean_review_text("Great service.  … More") == "Great service."
    assert clean_review_text("Line one\n\n  Line two  ") == "Line one\nLine two"
    assert clean_review_text("   ") is None      # empty stays empty, never ""
    assert clean_review_text(None) is None
    assert clean_name("  John   Smith ") == "John Smith"


def test_parse_gbp_review_maps_every_field():
    payload = {
        "reviewId": "AbC123",
        "reviewer": {
            "displayName": "John Smith",
            "profilePhotoUrl": "https://lh3.googleusercontent.com/a/x",
        },
        "starRating": "FIVE",
        "comment": "Excellent service department.",
        "createTime": "2026-08-19T14:15:00Z",
        "updateTime": "2026-08-19T14:15:00Z",
    }

    parsed = parse_gbp_review(payload, place_id="ChIJ0XFsWkI-kYgR1j9GfmxgynY")

    assert parsed["source_review_id"] == "AbC123"
    assert parsed["reviewer_name"] == "John Smith"
    assert parsed["rating"] == 5
    assert parsed["review_text"] == "Excellent service department."
    assert parsed["review_date"] == dt.datetime(2026, 8, 19, 14, 15)
    assert parsed["review_date_is_approximate"] is False
    assert parsed["source"] == "gbp_api"


def test_parse_gbp_review_with_a_rating_but_no_comment():
    """A star-only review is normal and must not become an empty-string body."""
    parsed = parse_gbp_review(
        {"reviewId": "x1", "starRating": "FOUR", "createTime": "2026-08-01T00:00:00Z"}
    )
    assert parsed["rating"] == 4
    assert parsed["review_text"] is None
    assert parsed["reviewer_name"] is None


def test_parse_maps_card():
    card = {
        "review_id": "ChdDSUhNMG9nS0VJQ0FnSUR",
        "rating_label": "5 stars",
        "reviewer_name": "Jane  Doe",
        "reviewer_profile_url": "https://www.google.com/maps/contrib/1234",
        "text": "Bought a new X5, smooth process. … More",
        "date_text": "2 weeks ago",
    }

    parsed = parse_maps_card(card, place_id="ChIJ0XFsWkI-kYgR1j9GfmxgynY")

    assert parsed["source_review_id"] == "ChdDSUhNMG9nS0VJQ0FnSUR"
    assert parsed["rating"] == 5
    assert parsed["reviewer_name"] == "Jane Doe"
    assert parsed["review_text"] == "Bought a new X5, smooth process."
    assert parsed["review_date_is_approximate"] is True
    assert parsed["source"] == "google_maps"


def test_parse_maps_card_with_everything_missing():
    """Requirement 4: absent fields stay null instead of being invented."""
    parsed = parse_maps_card({"review_id": "abc"})
    assert parsed["source_review_id"] == "abc"
    assert parsed["rating"] is None
    assert parsed["reviewer_name"] is None
    assert parsed["review_text"] is None
    assert parsed["review_date"] is None


def test_star_only_review_never_borrows_the_owners_reply():
    """Regression: a rating with no comment must come back as null.

    The Maps extractor used to fall back to a bare '.wiI7pd' selector, which on
    a star-only review matched the dealership's OWN reply and stored it as if
    the customer had written it. 34 of 403 collected reviews were affected.
    """
    parsed = parse_maps_card({"review_id": "abc", "rating_label": "5 stars", "text": None})
    assert parsed["review_text"] is None
    assert parsed["rating"] == 5


def test_owner_reply_marker_is_stripped_if_it_leaks_through():
    """Second line of defence, in case the reply ends up inside the comment node."""
    card = {
        "review_id": "abc",
        "rating_label": "5 stars",
        "text": "Great service!\nResponse from the owner\nThank you for taking the time!",
    }
    text = parse_maps_card(card)["review_text"]
    assert text is not None
    assert "Response from the owner" not in text
    assert "Thank you for taking the time" not in text
    assert text.startswith("Great service!")


# ---------------------------------------------------------------------------
# listing_identity: review links need the listing's feature id, or Google opens
# the review on a bare page with no dealership name.
# ---------------------------------------------------------------------------
from app.collector.parser import google_review_permalink, listing_identity  # noqa: E402

MERIT_URL = (
    "https://www.google.com/maps/place/Merit+Auto+Group/@30.313578,-81.5676933,17z/"
    "data=!3m1!4b1!4m6!3m5!1s0x88e5b53fc3070bdf:0x80dea25b3b72720a!8m2!3d30.3135!4d-81.5650"
    "!16s%2Fg%2F11fvgyzz_z"
)


def test_listing_identity_reads_feature_id_and_prefers_the_pin():
    found = listing_identity(MERIT_URL)
    assert found["feature_id"] == "0x88e5b53fc3070bdf:0x80dea25b3b72720a"
    # The pin, not the viewport centre the map happened to be scrolled to.
    assert (found["latitude"], found["longitude"]) == (30.3135, -81.5650)


def test_listing_identity_returns_nothing_for_a_place_id_link():
    found = listing_identity("https://www.google.com/maps/place/?q=place_id:ChIJ0XFsWkI")
    assert found == {"feature_id": None, "latitude": None, "longitude": None}
    assert listing_identity(None)["feature_id"] is None


def test_permalink_built_from_the_listing_url_names_the_dealership():
    found = listing_identity(MERIT_URL)
    url = google_review_permalink("Ci9DQUlR", **found)
    assert "0x0:0x0" not in url
    assert url.endswith("!2m1!1s0x88e5b53fc3070bdf:0x80dea25b3b72720a?hl=en")
    assert "/@30.3135,-81.565,17z/" in url
