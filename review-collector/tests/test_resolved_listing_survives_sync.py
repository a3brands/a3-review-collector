"""A listing the collector resolved must survive the registry sync.

Merit Auto Group was configured at /admin with a Google *search* link. The Maps
backend searched, landed on the listing and saved it into google_url -- and the
registry sync, which runs on every cycle, overwrote it with the search link
again. So every check searched afresh, and because Merit is a group with several
locations, Google intermittently answered with a list of candidates and the
check failed (4 times between 2026-09-11 and 2026-09-14, ~4.5 minutes each).
"""
from __future__ import annotations

from app.config import BusinessConfig
from app.database.database import session_scope, sync_businesses_from_config
from app.database.models import Business

SEARCH = "https://www.google.com/search?q=merit+auto+group"
LISTING = ("https://www.google.com/maps/place/Merit+Auto+Group/@30.313578,-81.5676933,17z/"
           "data=!3m1!4b1!4m6!3m5!1s0x88e5b53fc3070bdf:0x80dea25b3b72720a")
OTHER_SEARCH = "https://www.google.com/search?q=merit+auto+group+orlando"
OTHER_LISTING = "https://www.google.com/maps/place/Merit+Auto+Group+Orlando/@28.5,-81.3,17z"


def _responder_sends(monkeypatch, url):
    cfg = BusinessConfig(key="meritauto", name="Merit Auto Group", google_url=url, place_id=None)
    monkeypatch.setattr("app.collector.remote_registry.resolve_businesses",
                        lambda settings=None: [cfg])
    # None = the Responder's list was not consulted for deactivation, which
    # keeps this test about the URL and nothing else.
    monkeypatch.setattr("app.collector.remote_registry.keys_listed_remotely",
                        lambda settings=None: None)


def _merit():
    with session_scope() as session:
        row = session.query(Business).filter_by(key="meritauto").one()
        return row.google_url, row.configured_url


def _collector_resolves(url):
    # What collection_service does after the backend lands on a listing.
    with session_scope() as session:
        session.query(Business).filter_by(key="meritauto").one().google_url = url


def test_first_sync_stores_the_configured_link(fresh_db, monkeypatch):
    _responder_sends(monkeypatch, SEARCH)
    sync_businesses_from_config()
    assert _merit() == (SEARCH, SEARCH)


def test_the_same_search_link_keeps_the_resolved_listing(fresh_db, monkeypatch):
    _responder_sends(monkeypatch, SEARCH)
    sync_businesses_from_config()
    _collector_resolves(LISTING)

    sync_businesses_from_config()          # the next cycle's sync
    sync_businesses_from_config()          # and the one after

    assert _merit() == (LISTING, SEARCH)


def test_changing_the_link_at_admin_replaces_the_resolved_listing(fresh_db, monkeypatch):
    # /admin is still the source of truth: a different link means a different
    # business may be meant, so the old resolution must not linger.
    _responder_sends(monkeypatch, SEARCH)
    sync_businesses_from_config()
    _collector_resolves(LISTING)

    _responder_sends(monkeypatch, OTHER_SEARCH)
    sync_businesses_from_config()

    assert _merit() == (OTHER_SEARCH, OTHER_SEARCH)


def test_a_listing_link_from_admin_always_wins(fresh_db, monkeypatch):
    _responder_sends(monkeypatch, SEARCH)
    sync_businesses_from_config()
    _collector_resolves(LISTING)

    _responder_sends(monkeypatch, OTHER_LISTING)
    sync_businesses_from_config()

    assert _merit() == (OTHER_LISTING, OTHER_LISTING)
