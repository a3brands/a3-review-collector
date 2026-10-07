#!/usr/bin/env python3
"""Switch the address both services send email from.

    review-collector/.venv/bin/python scripts/switch_sender.py galaxy@a3brands.com

Asks for the Gmail App Password in a hidden prompt, signs in to Gmail with it
before changing anything, then writes it to both .env files (the dashboard and
the collector each send mail and each have their own copy). The password never
appears on screen, in shell history or in a chat transcript.

Both .env files are backed up to ~/a3-env-backups first, outside the project so
a backup can never be committed. The migration folder's copies are updated too
when it exists, so a machine set up from it sends from the same address.
"""
from __future__ import annotations

import datetime as dt
import getpass
import re
import shutil
import smtplib
import ssl
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANAGER_ENV = ROOT / "google-reviews-manager" / ".env"
COLLECTOR_ENV = ROOT / "review-collector" / ".env"
BUNDLE = Path.home() / "Desktop" / "a3-migration-2026-10-05"
BACKUPS = Path.home() / "a3-env-backups"

MANAGER_KEYS = ("SMTP_USER", "SMTP_APP_PASSWORD")
COLLECTOR_KEYS = ("SMTP_USERNAME", "SMTP_PASSWORD", "NOTIFY_FROM")


def set_value(text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", text, re.M):
        return re.sub(rf"^{key}=.*$", lambda _: line, text, count=1, flags=re.M)
    return text.rstrip("\n") + "\n" + line + "\n"


def verify(user: str, password: str) -> None:
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(user, password)


def write(env: Path, values: dict[str, str]) -> None:
    text = env.read_text(encoding="utf-8")
    for key, value in values.items():
        text = set_value(text, key, value)
    env.write_text(text, encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 2 or "@" not in sys.argv[1]:
        print(__doc__)
        return 2
    user = sys.argv[1].strip().lower()

    password = getpass.getpass(f"Gmail App Password for {user} (hidden): ").replace(" ", "").strip()
    if len(password) != 16:
        print(f"An App Password is 16 letters; that was {len(password)}. Nothing changed.")
        return 1

    print("Checking it with Gmail…")
    try:
        verify(user, password)
    except smtplib.SMTPAuthenticationError:
        print("Gmail refused that address and password. Nothing changed.")
        return 1
    except OSError as exc:
        print(f"Could not reach Gmail ({exc}). Nothing changed.")
        return 1
    print("Gmail accepted it.")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    BACKUPS.mkdir(mode=0o700, exist_ok=True)
    for env in (MANAGER_ENV, COLLECTOR_ENV):
        dest = BACKUPS / f"{env.parent.name}.env.{stamp}"
        shutil.copy2(env, dest)
        dest.chmod(0o600)
    print(f"Backed up both .env files to {BACKUPS}")

    manager = dict(zip(MANAGER_KEYS, (user, password)))
    collector = dict(zip(COLLECTOR_KEYS, (user, password, user)))
    targets = [(MANAGER_ENV, manager), (COLLECTOR_ENV, collector)]
    if BUNDLE.exists():
        targets += [
            (BUNDLE / "google-reviews-manager" / ".env", manager),
            (BUNDLE / "review-collector" / ".env", collector),
        ]
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
