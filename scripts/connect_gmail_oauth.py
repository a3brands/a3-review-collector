#!/usr/bin/env python3
"""Send the system's email from a Google Workspace account without an App Password.

    review-collector/.venv/bin/python scripts/connect_gmail_oauth.py galaxy@a3brands.com ~/Downloads/client_secret.json

For an account whose domain blocks 2-Step Verification, so App Passwords are
unavailable. Google's own sign-in page opens in the browser; sign in as that
account and allow access. The script then:

  1. signs in to Gmail's mail server with the new token, and stops if it fails
  2. backs up both .env files to ~/a3-env-backups (outside the project)
  3. writes the sender and token into both .env files, and the migration
     folder's copies when that folder exists
  4. restarts both services

Nothing changes unless step 1 succeeds. The token is never printed.
The client JSON comes from a Google Cloud "Desktop app" OAuth client.
"""
from __future__ import annotations

import base64
import datetime as dt
import http.server
import json
import secrets
import shutil
import smtplib
import ssl
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANAGER_ENV = ROOT / "google-reviews-manager" / ".env"
COLLECTOR_ENV = ROOT / "review-collector" / ".env"
BUNDLE = Path.home() / "Desktop" / "a3-migration-2026-10-05"
BACKUPS = Path.home() / "a3-env-backups"
SCOPE = "https://mail.google.com/"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from switch_sender import write  # noqa: E402


def load_client(path: Path) -> tuple[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    client = data.get("installed") or data.get("web") or {}
    if not client.get("client_id") or not client.get("client_secret"):
        raise SystemExit(f"{path} is not an OAuth client file (no client_id/client_secret).")
    if "installed" not in data:
        print("Note: this is not a 'Desktop app' client. If Google refuses the sign-in, create a Desktop app client.")
    return client["client_id"], client["client_secret"]


def get_code(client_id: str, user: str) -> tuple[str, str]:
    """Open Google's sign-in page and catch the reply on a local port."""
    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] == state:
                result["code"] = query.get("code", [""])[0]
                result["error"] = query.get("error", [""])[0]
            ok = bool(result.get("code"))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((
                "<h2 style='font-family:sans-serif'>"
                + ("Connected. You can close this tab and go back to Terminal." if ok
                   else "Not connected. Go back to Terminal for the reason.")
                + "</h2>").encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "login_hint": user,
        "state": state,
    })
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    print(f"\nOpening Google sign-in. Sign in as {user} and click Allow.")
    print(f"If no browser opens, paste this into one:\n{url}\n")
    webbrowser.open(url)
    thread.join(timeout=300)
    server.server_close()
    if result.get("error"):
        raise SystemExit(f"Google returned: {result['error']}. Nothing changed.")
    if not result.get("code"):
        raise SystemExit("No reply from Google within 5 minutes. Nothing changed.")
    return result["code"], redirect


def exchange(client_id: str, client_secret: str, code: str, redirect: str) -> dict:
    body = urllib.parse.urlencode({
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code",
    }).encode()
    with urllib.request.urlopen(
        urllib.request.Request("https://oauth2.googleapis.com/token", data=body), timeout=30
    ) as response:
        return json.load(response)


def verify(user: str, access_token: str) -> None:
    raw = f"user={user}\x01auth=Bearer {access_token}\x01\x01"
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        code, reply = smtp.docmd("AUTH", "XOAUTH2 " + base64.b64encode(raw.encode()).decode())
        if code != 235:
            raise SystemExit(f"Gmail refused the sign-in ({code}): {reply!r}. Nothing changed.")


def main() -> int:
    if len(sys.argv) != 3 or "@" not in sys.argv[1]:
        print(__doc__)
        return 2
    user = sys.argv[1].strip().lower()
    client_id, client_secret = load_client(Path(sys.argv[2]).expanduser())

    code, redirect = get_code(client_id, user)
    tokens = exchange(client_id, client_secret, code, redirect)
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise SystemExit("Google gave no refresh token. Remove the app's access at "
                         "myaccount.google.com/permissions and run this again. Nothing changed.")

    print("Checking it with Gmail…")
    verify(user, tokens["access_token"])
    print("Gmail accepted it.")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    for env in (MANAGER_ENV, COLLECTOR_ENV):
        dest = BACKUPS / f"{env.parent.name}.env.{stamp}"
        shutil.copy2(env, dest)
        dest.chmod(0o600)
    print(f"Backed up both .env files to {BACKUPS}")

    oauth = {"GMAIL_OAUTH_CLIENT_ID": client_id, "GMAIL_OAUTH_CLIENT_SECRET": client_secret,
             "GMAIL_OAUTH_REFRESH_TOKEN": refresh}
    manager = {"SMTP_USER": user, **oauth}
    collector = {"SMTP_USERNAME": user, "NOTIFY_FROM": user, **oauth}
    targets = [(MANAGER_ENV, manager), (COLLECTOR_ENV, collector)]
    if BUNDLE.exists():
        targets += [(BUNDLE / "google-reviews-manager" / ".env", manager),
                    (BUNDLE / "review-collector" / ".env", collector)]
    for env, values in targets:
        if env.exists():
            write(env, values)
            print(f"Updated {env}")

    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    for label in ("com.a3brands.reviewsmanager", "com.a3brands.reviewcollector"):
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"], check=False)
    print(f"Restarted both services. Email now goes out from {user}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
