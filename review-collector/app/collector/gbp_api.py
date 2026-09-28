"""Google Business Profile API backend -- the recommended path.

Why this is the primary backend:
  * It is free. Google: "The Google My Business API is available to registered
    users at no charge." No billing account, no per-call cost.
  * It is supported and stable, unlike scraping the Maps front-end.
  * It returns a real, permanent ``reviewId`` -- perfect de-duplication.
  * It returns reviews for a profile you manage, which is exactly the case here.

Requirements (all free, one-time):
  1. A Google Cloud project.
  2. Business Profile APIs enabled on that project.
  3. Access approved via the Business Profile API access request form.
  4. An OAuth refresh token for an account that manages the two dealerships.
     ``scripts/gbp_authorize.py`` walks through this.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

import httpx

from app.collector.base import (
    AccessBlocked,
    BackendUnavailable,
    CollectionResult,
    CollectorBackend,
    ParseError,
    RawReview,
    TransientCollectorError,
)
from app.collector.parser import parse_gbp_review
from app.config import Settings

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
REVIEWS_URL = "https://mybusiness.googleapis.com/v4/{location}/reviews"
ACCOUNTS_URL = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
LOCATIONS_URL = "https://mybusinessbusinessinformation.googleapis.com/v1/{account}/locations"
SCOPE = "https://www.googleapis.com/auth/business.manage"

MAX_PAGE_SIZE = 50


class GoogleBusinessProfileBackend(CollectorBackend):
    name = "gbp_api"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------
    def is_available(self) -> tuple[bool, str]:
        if not self.settings.has_gbp_credentials():
            return False, (
                "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN are not set. "
                "Run scripts/gbp_authorize.py to obtain a refresh token."
            )
        return True, "Google Business Profile API credentials present."

    # ------------------------------------------------------------------
    def _get_access_token(self) -> str:
        """Exchange the long-lived refresh token for a short-lived access token."""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        try:
            response = httpx.post(
                TOKEN_URL,
                data={
                    "client_id": self.settings.google_client_id,
                    "client_secret": self.settings.google_client_secret,
                    "refresh_token": self.settings.google_refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise TransientCollectorError(f"Could not reach Google's OAuth endpoint: {exc}") from exc

        if response.status_code in (400, 401):
            raise BackendUnavailable(
                "Google rejected the refresh token (HTTP "
                f"{response.status_code}). It was probably revoked or the OAuth client changed. "
                "Re-run scripts/gbp_authorize.py. Response: "
                f"{response.text[:300]}"
            )
        if response.status_code >= 500:
            raise TransientCollectorError(
                f"Google OAuth returned HTTP {response.status_code}; will retry."
            )
        response.raise_for_status()

        payload = response.json()
        self._access_token = payload["access_token"]
        self._token_expires_at = time.time() + int(payload.get("expires_in", 3600))
        return self._access_token

    # ------------------------------------------------------------------
    def _request(self, url: str, params: Optional[dict] = None) -> dict:
        token = self._get_access_token()
        try:
            response = httpx.get(
                url,
                params=params or {},
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.settings.collector_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise TransientCollectorError(f"Timed out calling {url}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransientCollectorError(f"Network error calling {url}: {exc}") from exc

        if response.status_code == 401:
            # Token might have just expired; drop it so the next attempt refreshes.
            self._access_token = None
            raise TransientCollectorError("Google returned 401 Unauthorized; token refreshed for retry.")
        if response.status_code == 403:
            raise AccessBlocked(
                "Google returned 403 Forbidden. Either the Business Profile API access request "
                "has not been approved yet (quota still shows 0 QPM in Cloud Console), or the "
                "authorised account does not manage this location. Response: "
                f"{response.text[:300]}"
            )
        if response.status_code == 404:
            raise BackendUnavailable(
                f"Google returned 404 for {url}. The configured GBP location name is wrong. "
                "Run scripts/gbp_list_locations.py to see the valid 'accounts/*/locations/*' values."
            )
        if response.status_code == 429:
            raise TransientCollectorError("Google returned 429 (rate limited); will retry next check.")
        if response.status_code >= 500:
            raise TransientCollectorError(f"Google returned HTTP {response.status_code}; will retry.")
        if response.status_code >= 400:
            raise ParseError(f"Unexpected HTTP {response.status_code} from Google: {response.text[:300]}")

        try:
            return response.json()
        except ValueError as exc:
            raise ParseError(f"Google returned a non-JSON body: {exc}") from exc

    # ------------------------------------------------------------------
    def list_accounts(self) -> List[dict]:
        """Helper used by scripts/gbp_list_locations.py."""
        return self._request(ACCOUNTS_URL, {"pageSize": 20}).get("accounts", [])

    def list_locations(self, account: str) -> List[dict]:
        """Helper used by scripts/gbp_list_locations.py."""
        return self._request(
            LOCATIONS_URL.format(account=account),
            {"pageSize": 100, "readMask": "name,title,storefrontAddress,metadata"},
        ).get("locations", [])

    # ------------------------------------------------------------------
    def collect(self, business, limit: int,
                known_review_count: Optional[int] = None) -> CollectionResult:
        # Ignored on purpose. The API returns the reviews in the same call that
        # would report the count, so there is nothing cheaper to stop at.
        location = (business.gbp_location_name or "").strip()
        if not location:
            raise BackendUnavailable(
                f"{business.name}: no GBP location configured. Set "
                f"{business.key.upper()}_GBP_LOCATION to an 'accounts/{{id}}/locations/{{id}}' value "
                "(scripts/gbp_list_locations.py prints them)."
            )
        if not location.startswith("accounts/"):
            raise BackendUnavailable(
                f"{business.name}: GBP location must look like "
                f"'accounts/123/locations/456', got {location!r}."
            )

        url = REVIEWS_URL.format(location=location)
        collected: List[RawReview] = []
        page_token: Optional[str] = None
        total_review_count: Optional[int] = None
        average_rating: Optional[float] = None

        while len(collected) < limit:
            params = {
                "pageSize": min(MAX_PAGE_SIZE, limit - len(collected)),
                # Newest first -- we only care about what arrived since last check.
                "orderBy": "updateTime desc",
            }
            if page_token:
                params["pageToken"] = page_token

            payload = self._request(url, params)
            if total_review_count is None:
                total_review_count = payload.get("totalReviewCount")
                average_rating = payload.get("averageRating")

            page = payload.get("reviews") or []
            for item in page:
                raw = RawReview(**parse_gbp_review(item, place_id=business.place_id))
                if raw.has_content() or raw.source_review_id:
                    collected.append(raw)

            page_token = payload.get("nextPageToken")
            if not page_token or not page:
                break

        logger.debug("%s: GBP API returned %d review(s)", business.name, len(collected))
        return CollectionResult(
            reviews=collected[:limit],
            backend=self.name,
            total_review_count=total_review_count,
            average_rating=average_rating,
        )
