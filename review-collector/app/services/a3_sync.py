"""Push new reviews to the A3 Review Responder.

Direction matters: the collector runs on a laptop behind NAT, so A3 on Railway
cannot reach it. The collector therefore pushes outbound, which needs no port
forwarding, no static IP and no tunnel.

The wire format is A3's, defined in its api/reviews-sync.js:

    POST /api/reviews-sync
    x-a3-timestamp: <unix seconds>
    x-a3-signature: sha256=<hex>
    { "payload": "{\\"reviews\\":[ ... ]}" }

    signature = HMAC-SHA256(A3_SYNC_SECRET, f"{timestamp}.{payload}")

The signed material is carried as a STRING because re-serialising a parsed JSON
object is not byte-stable, and A3 verifies the exact bytes it was given.

Two rules this module will not break:

  1. Send Google's RAW review id, never our namespaced `review_id`. A3 dedupes
     on Google's id against both `gbp_review_name` and `id`, and it has a second
     ingest path (Apify) that writes the raw value. Sending the prefixed form
     would create a second row for every review.
  2. Do not send reply state unless A3_SYNC_SEND_REPLY_STATE is on. A3's UPDATE
     writes `business_reply` without COALESCE, so a null from us would erase the
     reply text A3 already holds.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Dict, List, Optional, Sequence

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger("a3sync")

# A3 rejects a batch larger than this outright.
A3_MAX_ITEMS = 200


class A3SyncError(Exception):
    pass


class A3NotConfigured(A3SyncError):
    pass


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------
def to_a3_item(review: Dict, dealer_id: str, settings: Settings) -> Optional[Dict]:
    """Map one collector review onto A3's expected shape.

    Returns None when A3 would reject it anyway, so a whole batch is never
    spoiled by one unusable row.
    """
    google_id = review.get("google_review_id")
    if not google_id or google_id.startswith(("bmw_fwb:", "mb_fwb:")):
        # A fingerprint is not a Google id; A3 would store nonsense.
        return None

    rating = review.get("rating")
    if not isinstance(rating, int) or not (1 <= rating <= 5):
        return None  # A3 requires an integer 1-5

    item = {
        "dealer_id": dealer_id,
        "review_id": google_id,
        "rating": rating,
        "reviewer": review.get("reviewer_name") or "Google user",
        "review": review.get("review_text") or "",
        "review_date": review.get("review_date"),
        "google_review_url": review.get("review_url"),
        "source": "a3-review-collector",
    }

    if settings.a3_sync_send_reply_state:
        item["status"] = "replied" if review.get("owner_replied") else "awaiting_reply"
        if review.get("owner_reply"):
            item["business_reply"] = review["owner_reply"]
        if review.get("owner_reply_date"):
            item["reply_date"] = review["owner_reply_date"]

    return item


def sign(secret: str, timestamp: int, payload: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.{payload}".encode("utf-8"), hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def build_request(items: Sequence[Dict], settings: Settings, timestamp: Optional[int] = None):
    """Return (headers, body) exactly as A3 expects to receive them."""
    timestamp = int(timestamp if timestamp is not None else time.time())
    # separators removes incidental whitespace; the same string is what we sign.
    payload = json.dumps({"reviews": list(items)}, separators=(",", ":"), ensure_ascii=False)
    headers = {
        "Content-Type": "application/json",
        "x-a3-timestamp": str(timestamp),
        "x-a3-signature": sign(settings.a3_sync_secret, timestamp, payload),
    }
    return headers, {"payload": payload}


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def _post_batch(items: List[Dict], settings: Settings) -> Dict:
    headers, body = build_request(items, settings)

    last_error: Optional[Exception] = None
    for attempt in range(1, settings.a3_sync_max_retries + 2):
        try:
            response = httpx.post(
                settings.a3_sync_url,
                json=body,
                headers=headers,
                timeout=settings.a3_sync_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt <= settings.a3_sync_max_retries:
                logger.warning("A3 sync attempt %d failed (%s); retrying", attempt, exc)
                time.sleep(2 * attempt)
                continue
            raise A3SyncError(f"Could not reach A3 after retries: {exc}") from exc

        if response.status_code == 401:
            raise A3SyncError(
                "A3 rejected the signature (401). The A3_SYNC_SECRET must match on both "
                "sides, and this machine's clock must be within 5 minutes of A3's."
            )
        if response.status_code == 503:
            raise A3SyncError(
                "A3 reports sync_not_configured (503): A3_SYNC_SECRET is unset or shorter "
                "than 32 characters on the A3 side."
            )
        if response.status_code >= 500:
            last_error = A3SyncError(f"A3 returned HTTP {response.status_code}")
            if attempt <= settings.a3_sync_max_retries:
                time.sleep(2 * attempt)
                continue
            raise last_error
        if response.status_code >= 400:
            raise A3SyncError(
                f"A3 rejected the request ({response.status_code}): {response.text[:300]}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise A3SyncError(f"A3 returned a non-JSON body: {exc}") from exc

    raise A3SyncError(str(last_error) if last_error else "A3 sync failed")


def push(reviews: List[Dict], settings: Optional[Settings] = None, dry_run: bool = False) -> Dict:
    """Send reviews to A3. Returns a summary; never raises for one bad review."""
    settings = settings or get_settings()

    problem = settings.a3_sync_problem()
    if problem:
        raise A3NotConfigured(problem)

    items: List[Dict] = []
    skipped: List[str] = []
    id_map: Dict[str, str] = {}      # A3's review_id -> our review_id

    for review in reviews:
        dealer_id = settings.a3_dealer_id(review.get("business_key"))
        if not dealer_id:
            skipped.append(review.get("review_id", "?"))
            continue
        item = to_a3_item(review, dealer_id, settings)
        if item is None:
            skipped.append(review.get("review_id", "?"))
            continue
        items.append(item)
        id_map[item["review_id"]] = review["review_id"]

    if not items:
        return {"sent": 0, "skipped": len(skipped), "accepted": [], "results": [], "dry_run": dry_run}

    accepted: List[str] = []
    results: List[Dict] = []
    batch_size = max(1, min(settings.a3_sync_batch_size, A3_MAX_ITEMS))

    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        if dry_run:
            logger.info("[dry run] would send %d review(s) to A3", len(batch))
            continue

        body = _post_batch(batch, settings)
        for entry in body.get("results", []) or []:
            results.append(entry)
            action = entry.get("action")
            a3_id = entry.get("review_id") or entry.get("gbp_review_name")
            if action in ("created", "updated", "unchanged") and a3_id in id_map:
                accepted.append(id_map[a3_id])
            elif action == "error":
                logger.warning("A3 rejected a review: %s", entry.get("error"))

    logger.info(
        "A3 sync: %d prepared, %d accepted, %d skipped%s",
        len(items), len(accepted), len(skipped), " [dry run]" if dry_run else "",
    )
    return {
        "sent": len(items),
        "accepted": accepted,
        "skipped": len(skipped),
        "results": results,
        "dry_run": dry_run,
    }
