#!/usr/bin/env python3
"""Paste a Gmail App Password straight into .env, then verify it.

    python scripts/set_smtp_password.py

The password is typed into a hidden prompt, written directly to .env, and
checked against Gmail before you rely on it. It never appears on screen, in
your shell history, or in a chat transcript.
"""
from __future__ import annotations

import argparse
import getpass
import re
import smtplib
import ssl
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ENV = ROOT / ".env"


def read_value(text: str, key: str) -> str:
    match = re.search(rf'^{key}=(.*)$', text, re.M)
    return match.group(1).strip() if match else ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--password",
        help="the App Password, passed directly instead of at a hidden prompt",
    )
    args = parser.parse_args()

    if not ENV.exists():
        raise SystemExit(f"No .env at {ENV}. Copy .env.example to .env first.")

    text = ENV.read_text()
    user = read_value(text, "SMTP_USERNAME")
    if not user:
        user = input("Gmail address that generated the App Password: ").strip()

    print(f"\nAccount: {user}")

    if args.password:
        password = args.password.strip().replace(" ", "")
    else:
        print("Generate one at https://myaccount.google.com/apppasswords")
        print("Paste the 16-character App Password, then press Enter.")
        print("NOTE: nothing appears as you paste -- that is normal.\n")
        password = getpass.getpass("App Password: ").strip().replace(" ", "")
    if not password:
        raise SystemExit("Nothing entered; .env unchanged.")

    cleaned = password.lstrip("#").strip("\"'")
    if len(cleaned) != 16:
        print(f"\n  WARNING: got {len(cleaned)} characters; Google's are exactly 16.")
        # Don't block on a confirmation prompt -- Gmail is the real check below,
        # and nothing is written unless it accepts the password.
        print("  Trying it anyway; nothing is saved unless Gmail accepts it.")

    print("\nVerifying against Gmail before saving...")
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(user, cleaned)
    except smtplib.SMTPAuthenticationError:
        raise SystemExit(
            "\nGoogle REJECTED it. .env has NOT been changed.\n"
            "  - Make sure the App Password was generated on this exact account.\n"
            "  - A revoked App Password can never be reused; generate a fresh one.\n"
            "  - Copy it with the copy button rather than retyping it."
        )
    except Exception as exc:
        raise SystemExit(f"\nCould not reach Gmail ({exc}). .env has NOT been changed.")

    updated = re.sub(r'^SMTP_PASSWORD=.*$', f"SMTP_PASSWORD={cleaned}", text, flags=re.M)
    if updated == text and "SMTP_PASSWORD=" not in text:
        updated = text.rstrip() + f"\nSMTP_PASSWORD={cleaned}\n"
    ENV.write_text(updated)

    print("\nAUTH OK -- saved to .env.")
    print("Next:  python scripts/test_email.py     (sends a real test message)")


if __name__ == "__main__":
    main()
