#!/usr/bin/env python3
"""One-time OAuth flow for the Google Business Profile API.

Prints a GOOGLE_REFRESH_TOKEN to paste into .env. Everything here is free --
the Business Profile API carries no charge and needs no billing account.

Before running:
  1. Create a project at https://console.cloud.google.com/
  2. Enable these APIs on it:
       - Google My Business API
       - My Business Account Management API
       - My Business Business Information API
  3. Request API access (free, one-time approval):
       https://developers.google.com/my-business/content/prereqs
     You are approved once the quota shows 300 QPM instead of 0.
  4. Create an OAuth 2.0 Client ID of type "Desktop app" and put the client id
     and secret in .env as GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET.

Then:
    python scripts/gbp_authorize.py

Sign in with the Google account that MANAGES the two dealership profiles.
"""
from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/business.manage"
REDIRECT = "urn:ietf:wg:oauth:2.0:oob"


def main() -> None:
    settings = get_settings()
    client_id = settings.google_client_id
    client_secret = settings.google_client_secret

    if not client_id or not client_secret:
        raise SystemExit(
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env first.\n"
            "Create an OAuth 2.0 Client ID (type: Desktop app) in Google Cloud Console."
        )

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # forces a refresh_token to be issued
    }
    print("\n1. Open this URL in a browser and sign in with the account that")
    print("   manages BMW and Mercedes-Benz of Fort Walton Beach:\n")
    print("   " + AUTH_URL + "?" + urllib.parse.urlencode(params))
    print("\n2. Approve access, then copy the authorization code Google shows you.\n")

    code = input("Paste the authorization code here: ").strip()
    if not code:
        raise SystemExit("No code entered; aborting.")

    response = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise SystemExit(f"Token exchange failed (HTTP {response.status_code}):\n{response.text}")

    payload = response.json()
    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        raise SystemExit(
            "Google did not return a refresh token. Revoke this app's access at "
            "https://myaccount.google.com/permissions and run this script again."
        )

    print("\nSuccess. Add this line to your .env:\n")
    print(f"GOOGLE_REFRESH_TOKEN={refresh_token}\n")
    print("Then run: python scripts/gbp_list_locations.py")


if __name__ == "__main__":
    main()
