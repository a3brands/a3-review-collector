"""Dashboard asset fingerprint.

Shared by main.py (which stamps the URLs) and the admin API (which reports the
current value) so an already-open tab can notice it is running stale code.
"""
from __future__ import annotations

import hashlib

from app.config import BASE_DIR

DASHBOARD_DIR = BASE_DIR / "dashboard"


def asset_version() -> str:
    digest = hashlib.sha256()
    for name in ("app.js", "style.css"):
        path = DASHBOARD_DIR / name
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:8]
