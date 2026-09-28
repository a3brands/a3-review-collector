"""Backend selection.

COLLECTOR_BACKEND:
  gbp_api     -- official Google Business Profile API only (recommended)
  playwright  -- public Google Maps page only (no credentials, best effort)
  auto        -- gbp_api when credentials exist, otherwise playwright
"""
from __future__ import annotations

import logging
from typing import Dict, List

from app.collector.base import CollectorBackend
from app.collector.gbp_api import GoogleBusinessProfileBackend
from app.collector.google_maps import GoogleMapsBackend
from app.config import Settings

logger = logging.getLogger(__name__)

# Accept both the module-ish and the technology-ish spelling.
ALIASES = {
    "gbp": "gbp_api",
    "gbp_api": "gbp_api",
    "api": "gbp_api",
    "playwright": "playwright",
    "google_maps": "playwright",
    "maps": "playwright",
    "scraper": "playwright",
    "auto": "auto",
}

_cache: Dict[str, CollectorBackend] = {}


def _build(name: str, settings: Settings) -> CollectorBackend:
    if name not in _cache:
        _cache[name] = (
            GoogleBusinessProfileBackend(settings)
            if name == "gbp_api"
            else GoogleMapsBackend(settings)
        )
    return _cache[name]


def resolve_backend_order(settings: Settings) -> List[str]:
    """Which backends to try, in order."""
    choice = ALIASES.get((settings.collector_backend or "auto").strip().lower())
    if choice is None:
        logger.warning(
            "Unknown COLLECTOR_BACKEND=%r; falling back to 'auto'.", settings.collector_backend
        )
        choice = "auto"

    if choice == "auto":
        return ["gbp_api", "playwright"] if settings.has_gbp_credentials() else ["playwright"]
    return [choice]


def get_backends(settings: Settings) -> List[CollectorBackend]:
    return [_build(name, settings) for name in resolve_backend_order(settings)]


def get_backend_status(settings: Settings) -> List[dict]:
    """Availability of every backend -- surfaced on the dashboard and /health."""
    order = resolve_backend_order(settings)
    status = []
    for name in ("gbp_api", "playwright"):
        backend = _build(name, settings)
        try:
            available, reason = backend.is_available()
        except Exception as exc:  # pragma: no cover - defensive
            available, reason = False, f"Availability check raised: {exc}"
        status.append(
            {
                "name": name,
                "available": available,
                "reason": reason,
                "selected": name in order,
                "priority": order.index(name) if name in order else None,
            }
        )
    return status


def reset_backend_cache() -> None:
    _cache.clear()
