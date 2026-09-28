"""Guards for the three blockers found before merging with A3 Review Responder.

Each of these would put visibly wrong data into A3 if it regressed.
"""
from __future__ import annotations

import datetime as dt

from app.collector.base import RawReview
from app.collector.parser import clean_owner_reply, clean_review_text, parse_gbp_review, parse_maps_card
from app.database.database import session_scope
from app.database.models import Review
from tests.conftest import business_id, make_review


# --------------------------------------------------- blocker 1: review ids
def test_google_review_id_strips_our_namespace(fresh_db, service, stub_backend):
    """A3 dedupes on Google's raw id. Our prefix would create duplicate rows."""
    stub_backend.reviews_by_key["bmw_fwb"] = [
        make_review(1, source_review_id="ChZDSUhNMG9nS0VJQ0FnSUN5NnI3Zkd3EAE")
    ]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    with session_scope() as session:
        row = session.query(Review).one()
        assert row.review_id == "bmw_fwb:ChZDSUhNMG9nS0VJQ0FnSUN5NnI3Zkd3EAE"
        assert row.google_review_id == "ChZDSUhNMG9nS0VJQ0FnSUN5NnI3Zkd3EAE"
        assert row.to_a3_payload()["google_review_id"] == "ChZDSUhNMG9nS0VJQ0FnSUN5NnI3Zkd3EAE"


def test_both_dealerships_keep_distinct_local_ids_but_share_the_google_id(
    fresh_db, service, stub_backend
):
    """Same Google id at two dealerships: distinct locally, identical for A3."""
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1, source_review_id="SAME")]
    stub_backend.reviews_by_key["mb_fwb"] = [make_review(1, source_review_id="SAME")]
    service.run_cycle(trigger="test")

    with session_scope() as session:
        rows = session.query(Review).all()
        assert {r.review_id for r in rows} == {"bmw_fwb:SAME", "mb_fwb:SAME"}
        assert {r.google_review_id for r in rows} == {"SAME"}


def test_fingerprinted_ids_are_left_intact(fresh_db, service, stub_backend):
    """A fingerprint is not a Google id -- it must stay recognisable as ours."""
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1, native_id=False)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    with session_scope() as session:
        row = session.query(Review).one()
        assert row.google_review_id.startswith("bmw_fwb:fp_")


# --------------------------------------------------- blocker 2: owner replies
def test_owner_reply_is_collected_for_a3(fresh_db, service, stub_backend):
    stub_backend.reviews_by_key["bmw_fwb"] = [
        RawReview(
            source_review_id="r1", reviewer_name="Someone", rating=1,
            review_text="Bad experience.", review_date=dt.datetime(2026, 8, 1),
            owner_replied=True, owner_reply="We're sorry, please call us.",
            owner_reply_date=dt.datetime(2026, 8, 2), source="stub",
        )
    ]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    with session_scope() as session:
        row = session.query(Review).one()
        assert row.owner_replied is True
        assert row.owner_reply == "We're sorry, please call us."
        assert row.review_text == "Bad experience."      # kept separate
        payload = row.to_a3_payload()
        assert payload["owner_replied"] is True
        assert payload["owner_reply"] == "We're sorry, please call us."


def test_review_without_an_owner_reply_is_marked_unreplied(fresh_db, service, stub_backend):
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    with session_scope() as session:
        row = session.query(Review).one()
        assert row.owner_replied is False
        assert row.owner_reply is None


def test_reply_presence_detected_even_when_google_hides_the_text(fresh_db):
    """The reviews tab often renders only the header, not the reply body.

    Presence alone is enough for A3 to mark a review 'posted'.
    """
    parsed = parse_maps_card({
        "review_id": "x", "rating_label": "5 stars", "text": "Great!",
        "owner_replied": True, "owner_reply": None,
        "owner_reply_date_text": "2 months ago",
    })
    assert parsed["owner_replied"] is True
    assert parsed["owner_reply"] is None
    assert parsed["owner_reply_date"] is not None


def test_owner_reply_never_leaks_into_the_customers_comment():
    """Grace's rule: the email must show the customer's words, not ours."""
    leaked = "Great service!\nResponse from the owner\nThank you for taking the time!"
    assert "Thank you for taking the time" not in (clean_review_text(leaked) or "")
    # ...but as an owner reply it is preserved verbatim.
    assert clean_owner_reply("Thank you for taking the time!") == "Thank you for taking the time!"


def test_gbp_api_reply_is_mapped(fresh_db):
    parsed = parse_gbp_review({
        "reviewId": "abc", "starRating": "TWO", "comment": "Not great",
        "reviewReply": {"comment": "Sorry to hear this.", "updateTime": "2026-07-01T10:00:00Z"},
    })
    assert parsed["owner_replied"] is True
    assert parsed["owner_reply"] == "Sorry to hear this."
    assert parsed["owner_reply_date"] == dt.datetime(2026, 7, 1, 10, 0)


# --------------------------------------------------- A3 field requirements
def test_every_stored_review_would_pass_a3_validation(fresh_db, service, stub_backend):
    """A3 requires dealer_id, a Google review id, and an integer rating 1-5."""
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(i) for i in range(1, 4)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    with session_scope() as session:
        for row in session.query(Review).all():
            payload = row.to_a3_payload()
            assert payload["google_review_id"]
            assert isinstance(payload["rating"], int)
            assert 1 <= payload["rating"] <= 5
            assert payload["business_key"] in ("bmw_fwb", "mb_fwb")   # maps to dealer_id


# --------------------------------------------------- refresh on re-check
def test_owner_reply_posted_later_is_picked_up(fresh_db, service, stub_backend):
    """The dealership usually replies days after the review appears."""
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    with session_scope() as session:
        assert session.query(Review).one().owner_replied is False

    replied = make_review(1)
    replied.owner_replied = True
    replied.owner_reply = "Thanks for the kind words!"
    stub_backend.reviews_by_key["bmw_fwb"] = [replied]

    result = service.check_business(business_id("bmw_fwb"), trigger="test")

    assert result["new_reviews"] == 0          # not a new review...
    assert result["updated_existing"] == 1     # ...but it did change
    with session_scope() as session:
        row = session.query(Review).one()
        assert row.owner_replied is True
        assert row.owner_reply == "Thanks for the kind words!"


def test_rating_edited_by_the_customer_is_detected(fresh_db, service, stub_backend):
    """5 stars edited down to 1 must not go unnoticed."""
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1, rating=5)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1, rating=1)]
    result = service.check_business(business_id("bmw_fwb"), trigger="test")

    assert result["updated_existing"] == 1
    with session_scope() as session:
        assert session.query(Review).one().rating == 1


def test_a_truncated_reread_never_replaces_the_full_comment(fresh_db, service, stub_backend):
    """A failed 'See more' expansion must not shorten stored text."""
    full = "A really detailed review that goes on for quite a while about the service."
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1, review_text=full)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1, review_text="A really detailed…")]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    with session_scope() as session:
        assert session.query(Review).one().review_text == full


def test_unchanged_reviews_are_not_counted_as_updated(fresh_db, service, stub_backend):
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1), make_review(2)]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    result = service.check_business(business_id("bmw_fwb"), trigger="test")

    assert result["updated_existing"] == 0
    assert result["skipped_existing"] == 2


def test_refreshing_does_not_requeue_a_processed_review(fresh_db, service, stub_backend):
    """A3 has already handled it -- an update must not resend it."""
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]
    service.check_business(business_id("bmw_fwb"), trigger="test")
    with session_scope() as session:
        session.query(Review).one().processed = True

    replied = make_review(1)
    replied.owner_replied = True
    stub_backend.reviews_by_key["bmw_fwb"] = [replied]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    with session_scope() as session:
        row = session.query(Review).one()
        assert row.processed is True          # still done
        assert row.owner_replied is True      # but refreshed


# --------------------------------------------------- already-answered policy
def test_already_answered_reviews_are_not_queued_for_a3(fresh_db, service, stub_backend):
    """A reply is live on Google -- there is nothing for the team to draft."""
    answered = make_review(1)
    answered.owner_replied = True
    stub_backend.reviews_by_key["bmw_fwb"] = [answered, make_review(2)]

    service.check_business(business_id("bmw_fwb"), trigger="test")

    with session_scope() as session:
        rows = {r.review_id: r for r in session.query(Review).all()}
        assert rows["bmw_fwb:greview-1"].processed is True    # answered -> closed
        assert rows["bmw_fwb:greview-2"].processed is False   # still needs a reply


def test_a_late_reply_closes_a_queued_review(fresh_db, service, stub_backend):
    stub_backend.reviews_by_key["bmw_fwb"] = [make_review(1)]
    service.check_business(business_id("bmw_fwb"), trigger="test")
    with session_scope() as session:
        assert session.query(Review).one().processed is False

    replied = make_review(1)
    replied.owner_replied = True
    stub_backend.reviews_by_key["bmw_fwb"] = [replied]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    with session_scope() as session:
        row = session.query(Review).one()
        assert row.processed is True
        assert row.processed_at is not None


# --------------------------------------------------- per-review deep links
def test_each_review_links_to_itself_on_google(fresh_db, service, stub_backend):
    """View used to open the dealership's whole review list, leaving you to
    scroll for the one you clicked."""
    from app.collector.parser import google_review_permalink

    stub_backend.reviews_by_key["bmw_fwb"] = [
        make_review(1, source_review_id="ChZDSUhNMG9nS0VJQ0FnSURmXzd6b2ZnEAE")
    ]
    service.check_business(business_id("bmw_fwb"), trigger="test")

    with session_scope() as session:
        url = session.query(Review).one().review_url
    assert "ChZDSUhNMG9nS0VJQ0FnSURmXzd6b2ZnEAE" in url
    assert "/maps/reviews/" in url
    assert "local/reviews?placeid" not in url        # not the whole list


def test_the_link_opens_on_the_dealerships_listing(fresh_db, service, stub_backend):
    """Without the listing's feature id the review opens as a bare page with no
    dealership name, no pin and no context."""
    from app.collector.parser import google_review_permalink

    anchored = google_review_permalink(
        "ChdABC", feature_id="0x88913e425a6c71d1:0x76ca606c7e463fd6",
        latitude=30.4538221, longitude=-86.6391596)
    assert "0x88913e425a6c71d1:0x76ca606c7e463fd6" in anchored
    assert "@30.4538221,-86.6391596" in anchored
    assert "0x0:0x0" not in anchored

    # No listing identity: still a working review link, just unanchored.
    assert "0x0:0x0" in google_review_permalink("ChdABC")


def test_no_link_is_invented_without_a_real_google_id(fresh_db):
    """A fingerprinted id would build a link that goes nowhere."""
    from app.collector.parser import google_review_permalink

    assert google_review_permalink(None) is None
    assert google_review_permalink("") is None
