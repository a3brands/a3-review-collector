"""API-key authentication for everything A3 and the dashboard call.

The key only ever comes from the A3_API_KEY environment variable -- it is never
written into source. If it is unset the API refuses every request rather than
silently running wide open.
"""
from __future__ import annotations

import hmac
import logging

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False, description="A3_API_KEY as a Bearer token")


def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> str:
    expected = (settings.a3_api_key or "").strip()

    if not expected:
        logger.error("A3_API_KEY is not set; refusing the request. Set it in .env.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A3_API_KEY is not configured on the collector. Set it in .env and restart.",
        )

    presented = ""
    if credentials and (credentials.scheme or "").lower() == "bearer":
        presented = (credentials.credentials or "").strip()
    else:
        # Convenience for curl / dashboards that prefer a plain header.
        presented = (request.headers.get("X-API-Key") or "").strip()

    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing credentials. Send 'Authorization: Bearer <A3_API_KEY>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not hmac.compare_digest(presented, expected):
        logger.warning("Rejected API request with an invalid key from %s", request.client.host if request.client else "?")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key.",
        )

    return presented
