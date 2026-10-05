"""Review links must open the review on the dealership's own listing.

Without the listing's feature id, Google opens the review on a bare page with
no dealership name. Four of six dealerships shipped 3,624 links like that
because the id was only ever typed in by hand.
"""
from __future__ import annotations

from app.database.database import session_scope
from app.database.models import Business, Review
from app.services import heartbeat
from tests.conftest import business_id, make_review

FEATURE = "0x88e5b53fc3070bdf:0x80dea25b3b72720a"
LISTING = (
    "https://www.google.com/maps/place/Merit+Auto+Group/@30.31,-81.56,17z/"
    f"data=!3m1!4b1!4m6!3m5!1s{FEATURE}!8m2!3d30.3135!4d-81.565"
)


def _configure(key, *, google_url, feature_id=None):
    with session_scope() as session:
        b = session.query(Business).filter_by(key=key).one()
        b.google_url = google_url
        b.configured_url = google_url
        b.feature_id = feature_id
        b.latitude = b.longitude = None


def _links(key):
    with session_scope() as session:
        return [r.review_url for r in session.query(Review).filter(
            Review.business_id == business_id(key))]


def _flagged():
    return [p["business_name"] for p in heartbeat.link_problems()]


def test_listing_id_is_read_from_the_configured_url(fresh_db, service, stub_backend):
    _configure("bmw_fwb", google_url=LISTING)
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]

    service.check_business(business_id("bmw_fwb"), trigger="test")

    [link] = _links("bmw_fwb")
    assert FEATURE in link and "0x0:0x0" not in link


def test_a_later_check_repairs_links_stored_without_the_id(fresh_db, service, stub_backend):
    _configure("bmw_fwb", google_url="https://www.google.com/maps/place/?q=place_id:ChIJx")
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]
    service.check_business(business_id("bmw_fwb"), trigger="test")
    assert "0x0:0x0" in _links("bmw_fwb")[0]
    assert "BMW of Fort Walton Beach" in _flagged()

    # The full listing URL arrives later (configured, or loaded by the scraper).
    _configure("bmw_fwb", google_url=LISTING)
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(2)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    links = _links("bmw_fwb")
    assert len(links) == 2
    assert all(FEATURE in link for link in links)
    assert "BMW of Fort Walton Beach" not in _flagged()
