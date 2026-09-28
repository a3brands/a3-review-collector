#!/usr/bin/env python3
"""List every Business Profile location the authorised account manages.

Prints the exact 'accounts/{id}/locations/{id}' values for
BMW_FWB_GBP_LOCATION and MB_FWB_GBP_LOCATION in .env.

    python scripts/gbp_list_locations.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collector.base import CollectorError  # noqa: E402
from app.collector.gbp_api import GoogleBusinessProfileBackend  # noqa: E402
from app.config import get_settings  # noqa: E402


def main() -> None:
    settings = get_settings()
    backend = GoogleBusinessProfileBackend(settings)

    available, reason = backend.is_available()
    if not available:
        raise SystemExit(f"Cannot query the API: {reason}")

    try:
        accounts = backend.list_accounts()
    except CollectorError as exc:
        raise SystemExit(f"Failed to list accounts: {exc}")

    if not accounts:
        raise SystemExit("The authorised account manages no Business Profile accounts.")

    for account in accounts:
        name = account.get("name", "")
        print(f"\n=== {name}  ({account.get('accountName', '?')}) ===")
        try:
            locations = backend.list_locations(name)
        except CollectorError as exc:
            print(f"  Could not list locations: {exc}")
            continue

        if not locations:
            print("  (no locations)")
        for location in locations:
            address = location.get("storefrontAddress", {}) or {}
            lines = address.get("addressLines", []) or []
            city = address.get("locality", "")
            region = address.get("administrativeArea", "")
            print(f"  {name}/{location.get('name', '')}")
            print(f"      title   : {location.get('title', '')}")
            print(f"      address : {', '.join(lines)} {city} {region}".rstrip())

    print(
        "\nPaste the matching values into .env, e.g.:\n"
        "  BMW_FWB_GBP_LOCATION=accounts/123456789/locations/987654321\n"
        "  MB_FWB_GBP_LOCATION=accounts/123456789/locations/123123123\n"
        "Then set COLLECTOR_BACKEND=gbp_api"
    )


if __name__ == "__main__":
    main()
