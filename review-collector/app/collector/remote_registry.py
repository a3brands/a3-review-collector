"""Where the dealership list comes from.

It used to be two hardcoded blocks in Settings -- bmw_fwb_* and mb_fwb_* -- so
adding a customer meant editing .env on this machine and restarting. Anyone
adding a dealership in the Responder's /admin screen got a dashboard, a login,
and no reviews, with nothing saying why.

The Responder is now the single place a dealership is defined, and this fetches
that list. Config stays as the fallback: if the Responder is unreachable, the
collector keeps checking whatever it already knows rather than going quiet.

Authenticated with the same HMAC secret as the outbound push, in the same shape,
because it is the same pair of machines talking in the other direction.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import List, Optional

import httpx

from app.config import BusinessConfig, Settings, get_settings

logger = logging.getLogger("remote_registry")

PATH = "/api/collector/businesses"


def _signature(secret: str, timestamp: int, payload: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.{payload}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def remote_configured(settings: Optional[Settings] = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.grm_url) and len(settings.grm_ingest_secret) >= 32


def fetch_businesses(settings: Optional[Settings] = None) -> Optional[List[BusinessConfig]]:
    """The dealerships the Responder knows about, or None if it could not say.

    None and [] mean different things and are treated differently by the caller:
    None is "could not ask", which must never deactivate anything; an empty list
    is a real answer.
    """
    settings = settings or get_settings()
    if not remote_configured(settings):
        return None

    url = settings.grm_url.rstrip("/") + PATH
    timestamp = int(time.time())

    try:
        response = httpx.get(
            url,
            timeout=settings.grm_timeout_seconds,
            headers={
                "X-A3-Timestamp": str(timestamp),
                # A GET has no body, so the path itself is what is signed.
                "X-A3-Signature": "sha256=" + _signature(
                    settings.grm_ingest_secret, timestamp, PATH
                ),
            },
        )
    except httpx.HTTPError as exc:
        logger.warning("Could not read the dealership list from the Responder: %s", exc)
        return None

    if response.status_code != 200:
        logger.warning(
            "Responder refused the dealership list (%s): %s",
            response.status_code,
            response.text[:200],
        )
        return None

    try:
        payload = response.json()
        rows = payload.get("businesses") or []
    except ValueError:
        logger.warning("Responder returned something that was not JSON")
        return None

    out: List[BusinessConfig] = []
    for row in rows:
        key = str(row.get("key") or "").strip()
        if not key:
            continue
        # The Responder's slugs contain hyphens; this side has always used
        # underscores in business keys (bmw_fwb), and those keys appear in
        # review fingerprints. Normalising here keeps both sides stable.
        out.append(
            BusinessConfig(
                key=key.replace("-", "_"),
                name=row.get("name") or key,
                google_url=row.get("google_url"),
                place_id=row.get("place_id"),
                gbp_location_name=row.get("gbp_location_name"),
            )
        )

    logger.info("Responder lists %d dealership(s): %s",
                len(out), ", ".join(b.key for b in out) or "none")
    return out


def resolve_businesses(settings: Optional[Settings] = None) -> List[BusinessConfig]:
    """The list to actually check this cycle.

    Remote first, config as the fallback. Merged rather than replaced, so a
    dealership configured in .env but not yet added to the Responder keeps
    working -- the two original dealerships are exactly that case until somebody
    fills in their Place ID on the admin screen.
    """
    settings = settings or get_settings()
    local = settings.businesses()
    remote = fetch_businesses(settings)

    if remote is None:
        return local

    by_key = {b.key: b for b in local}
    for business in remote:
        # Remote wins on identity: the admin screen is where somebody just
        # changed it, so it is the more recent answer.
        by_key[business.key] = business
    return list(by_key.values())


def keys_listed_remotely(settings: Optional[Settings] = None) -> Optional[set]:
    """The exact set of keys the Responder currently lists, or None if it could
    not be asked.

    None and an empty set mean different things. None means the question went
    unanswered and nothing should be deactivated on the strength of it. An
    answer that simply omits a dealership is definitive: somebody deactivated it
    or removed its listing, and continuing to check it wastes a slot every cycle
    and keeps health permanently degraded.
    """
    remote = fetch_businesses(settings)
    return None if remote is None else {b.key for b in remote}
