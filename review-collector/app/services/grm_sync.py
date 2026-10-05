"""Push collected reviews into the Google Reviews Manager (the Railway app).

Why this direction: the manager runs on Railway and cannot reach this machine,
which sits behind NAT. So the collector calls out to it.

Why it exists at all: the manager reads Google reviews through the Business
Profile API, and that access is not approved yet -- so on its own it can only
show mock data. This collector reads the public listings instead, which needs no
approval, making it currently the only source of real reviews for both the
manager's dashboard and (through the manager's own a3-sync) the A3 Review
Responder.

Wire format matches the manager's src/collector-ingest.js, which in turn matches
its existing a3-sync.js:

    POST {GRM_URL}/api/ingest/{dealer-slug}/google
    x-a3-timestamp: <unix seconds>
    x-a3-signature: sha256=<hex>
    { "payload": "{\\"reviews\\":[ ... ], \\"business_info\\": { ... }}" }

    signature = HMAC-SHA256(GRM_INGEST_SECRET, f"{timestamp}.{payload}")

The payload is signed as a STRING because Express parses the JSON body before a
handler sees the raw bytes, and re-serialising a parsed object is not
byte-stable.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Dict, List, Optional

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger("grmsync")


class GRMSyncError(Exception):
    pass


class GRMNotConfigured(GRMSyncError):
    pass


def to_grm_review(review: Dict) -> Dict:
    """Map one collector review onto the shape review-store.js normalises.

    Field names are the ones its normalizeReview() already accepts, so nothing
    on the manager side needs to learn a new format.
    """
    return {
        "name": review.get("reviewer_name") or "Anonymous",
        "stars": review.get("rating"),
        "text": review.get("review_text") or "",
        "publishedAtDate": review.get("review_date"),
        # Google's public listing gives ages, not dates: "2 months ago" becomes a
        # timestamp of scrape-time-minus-two-months. Sent on so the manager can
        # say "about 2 months ago" instead of inventing a day and an hour.
        # Without it, 94% of BMW's review dates read as exact when 47 of them
        # share a single millisecond.
        #
        # The derived timestamp stays usable as it ages: it is an origin, so
        # now - review_date grows at the right rate and the age stays true.
        "dateIsApproximate": bool(review.get("review_date_is_approximate")),
        "reviewUrl": review.get("review_url"),
        # When the dealership answered. The manager could not report a reply
        # time at all without this, because nothing else carries the date.
        "ownerReplyDate": review.get("owner_reply_date"),
        # The manager shows the owner's reply when it has one. We can only
        # detect that a reply exists, not read its text, so send nothing rather
        # than an empty string that would render as a blank reply.
        "responseFromOwnerText": review.get("owner_reply") or None,
        # We can detect that the dealership replied without being able to read
        # the text. Reporting that as "no reply" would show a 0% response rate
        # for a dealer who answers nearly everything.
        "hasOwnerReply": bool(review.get("owner_replied")),
        # Google's own id, so the manager (and A3 beyond it) can deduplicate.
        "reviewId": review.get("google_review_id"),
    }


def sign(secret: str, timestamp: int, payload: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.{payload}".encode("utf-8"), hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def build_request(reviews: List[Dict], business_info: Dict, settings: Settings,
                  timestamp: Optional[int] = None):
    timestamp = int(timestamp if timestamp is not None else time.time())
    payload = json.dumps(
        {
            "reviews": [to_grm_review(r) for r in reviews],
            "business_info": business_info,
            "review_summary": {"pulled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    headers = {
        "Content-Type": "application/json",
        "x-a3-timestamp": str(timestamp),
        "x-a3-signature": sign(settings.grm_ingest_secret, timestamp, payload),
    }
    return headers, {"payload": payload}


def push_dealer(dealer_slug: str, reviews: List[Dict], business_info: Dict,
                settings: Optional[Settings] = None, dry_run: bool = False) -> Dict:
    """Send one dealership's reviews. Replaces that dealer's Google file."""
    settings = settings or get_settings()

    problem = settings.grm_problem()
    if problem:
        raise GRMNotConfigured(problem)

    headers, body = build_request(reviews, business_info, settings)
    url = f"{settings.grm_url.rstrip('/')}/api/ingest/{dealer_slug}/google"

    if dry_run:
        logger.info("[dry run] would push %d review(s) to %s", len(reviews), url)
        return {"ok": True, "dry_run": True, "reviews": len(reviews), "url": url}

    last_error: Optional[Exception] = None
    for attempt in range(1, settings.grm_max_retries + 2):
        try:
            response = httpx.post(url, json=body, headers=headers,
                                  timeout=settings.grm_timeout_seconds)
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt <= settings.grm_max_retries:
                logger.warning("GRM push attempt %d failed (%s); retrying", attempt, exc)
                time.sleep(2 * attempt)
                continue
            raise GRMSyncError(f"Could not reach the reviews manager: {exc}") from exc

        if response.status_code == 401:
            raise GRMSyncError(
                "The reviews manager rejected the signature (401). GRM_INGEST_SECRET must "
                "match COLLECTOR_INGEST_SECRET there, and this machine's clock must be "
                "within 5 minutes of the server's."
            )
        if response.status_code == 503:
            raise GRMSyncError(
                "The reviews manager reports ingest_not_configured (503): "
                "COLLECTOR_INGEST_SECRET is unset or shorter than 32 characters there."
            )
        if response.status_code == 404:
            raise GRMSyncError(
                f"The reviews manager does not know dealer '{dealer_slug}' (404). "
                "Check GRM_DEALER_BMW_FWB / GRM_DEALER_MB_FWB against its src/dealers.js."
            )
        if response.status_code >= 500:
            last_error = GRMSyncError(f"Reviews manager returned HTTP {response.status_code}")
            if attempt <= settings.grm_max_retries:
                time.sleep(2 * attempt)
                continue
            raise last_error
        if response.status_code >= 400:
            raise GRMSyncError(
                f"Reviews manager rejected the push ({response.status_code}): {response.text[:300]}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise GRMSyncError(f"Reviews manager returned a non-JSON body: {exc}") from exc

    raise GRMSyncError(str(last_error) if last_error else "push failed")
