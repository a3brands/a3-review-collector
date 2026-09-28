#!/usr/bin/env python3
"""Nightly backup of everything the business owns.

    python3 scripts/backup.py          # run once, now
    ./scripts/restore-check.sh         # prove the newest backup restores

Three things, because losing any one of them loses the business:

  1. Postgres (a3_reviews)         reviews, drafts awaiting approval, approvals
  2. dealers.json / accounts.json  dealerships, customers, logins
  3. the collector's SQLite        collection history and per-dealership pacing

Written in Python and run by the collector's own interpreter rather than as a
shell script, for a reason that is not obvious: macOS refuses to let a
LaunchAgent execute a script under ~/Desktop, and refuses to let /bin/bash READ
anything under ~/Desktop even when the script itself lives elsewhere. A binary
that lives under ~/Desktop, such as the collector's venv python, is allowed
both. The same trap is documented in com.a3brands.reviewtunnel.plist.

Deliberately plain: it copies, it does not encrypt, and it never deletes or
modifies anything it did not create. An earlier shell version encrypted the
archive and removed the originals, which is the shape of ransomware and was
quarantined by security software on this machine. Encryption earns its keep once
backups leave the machine; while they sit on the same Mac it buys nothing.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ROOT") or Path(__file__).resolve().parent.parent)
RESPONDER = ROOT / "google-reviews-manager"
COLLECTOR = ROOT / "review-collector"

# Outside the project folder on purpose. A backup living inside the thing it
# backs up does not survive that folder being lost, moved or replaced.
DEST = Path(os.environ.get("BACKUP_DIR") or Path.home() / "Backups" / "a3-review-system")
KEEP = int(os.environ.get("KEEP", "14"))

PG_BIN = Path("/Applications/Postgres.app/Contents/Versions/latest/bin")


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}] {message}", flush=True)


def read_env(path: Path, key: str) -> str | None:
    """One value out of a .env, read as data.

    A .env is not a shell script. Sourcing one breaks on any value containing a
    space, and a value containing $(...) would be executed.
    """
    try:
        for line in path.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def pg_dump(database_url: str, out: Path) -> bool:
    exe = PG_BIN / "pg_dump" if (PG_BIN / "pg_dump").exists() else Path("pg_dump")
    try:
        subprocess.run(
            [str(exe), "--format=custom", "--no-owner", "--no-privileges",
             f"--file={out}", database_url],
            check=True, capture_output=True, text=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        print(f"  ERROR: pg_dump failed: {detail.strip()[:300]}", file=sys.stderr)
        return False


def pg_count(database_url: str, table: str) -> int | None:
    exe = PG_BIN / "psql" if (PG_BIN / "psql").exists() else Path("psql")
    try:
        done = subprocess.run(
            [str(exe), "-tAc", f"SELECT count(*) FROM {table}", database_url],
            check=True, capture_output=True, text=True,
        )
        return int(done.stdout.strip())
    except Exception:
        return None


def copy_sqlite(source: Path, out: Path) -> int | None:
    """A consistent copy of a live SQLite database.

    sqlite3's own backup API rather than a file copy: the database runs in WAL
    mode, so copying the .db alone can miss commits still sitting in the log.
    The copy is then switched to a plain journal, because a WAL database cannot
    be opened read only without somewhere to create its shared-memory file, and
    an archived file that cannot be opened read only is a poor backup.
    """
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(out)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            return dst.execute("SELECT count(*) FROM reviews").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()


def already_backed_up_today() -> Path | None:
    """The newest archive taken today, if there is one.

    The schedule is RunAtLoad rather than a fixed nightly hour, because this Mac
    is shut down overnight and a 03:15 StartCalendarInterval never fired once --
    launchd does not make up a missed calendar run for a machine that was off.
    Firing at login instead means the backup happens whenever the day actually
    starts, at the cost of also firing on every other login and reboot, which is
    what this guard absorbs.
    """
    today = datetime.now().strftime("%Y%m%d")
    matches = sorted(DEST.glob(f"a3-{today}T*.tar.gz"))
    return matches[-1] if matches else None


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    DEST.mkdir(parents=True, exist_ok=True)

    # FORCE=1, or any argument, takes a second backup on a day that already has
    # one -- for a by-hand run before something risky.
    forced = bool(sys.argv[1:]) or os.environ.get("FORCE") == "1"
    existing = already_backed_up_today()
    if existing and not forced:
        log(f"already backed up today ({existing.name}), nothing to do")
        return 0

    log("backup starting")

    database_url = os.environ.get("DATABASE_URL") or read_env(RESPONDER / ".env", "DATABASE_URL")
    manifest: dict = {"taken_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    problems: list[str] = []

    with tempfile.TemporaryDirectory(dir=DEST) as tmp:
        work = Path(tmp)

        # ---- 1. Postgres -------------------------------------------------
        if database_url:
            dump = work / "a3_reviews.dump"
            if pg_dump(database_url, dump):
                log(f"  postgres  {dump.stat().st_size // 1024}K")
                for table in ("reviews", "pending_replies", "approved_replies"):
                    manifest[f"postgres_{table}"] = pg_count(database_url, table)
            else:
                problems.append("Postgres was NOT backed up")
        else:
            problems.append("DATABASE_URL not found, Postgres was NOT backed up")

        # ---- 2. The files that are still state ---------------------------
        for name in ("accounts.json", "dealers.json"):
            source = RESPONDER / "data" / name
            key = f"has_{name.split('.')[0]}"
            if source.exists():
                shutil.copy2(source, work / name)
                manifest[key] = True
                log(f"  {name}")
            else:
                manifest[key] = False
                # accounts.json only exists once a customer login is created, so
                # its absence is a fact about the system, not a failure.
                log(f"  note: data/{name} is not present")

        # ---- 3. The collector's database ---------------------------------
        collector_db = COLLECTOR / "data" / "reviews.db"
        if collector_db.exists():
            try:
                rows = copy_sqlite(collector_db, work / "collector.db")
                manifest["collector_reviews"] = rows
                log(f"  collector {(work / 'collector.db').stat().st_size // 1024}K, {rows} review(s)")
            except sqlite3.Error as exc:
                problems.append(f"collector database could not be copied: {exc}")
        else:
            problems.append("the collector database is not where it was expected")

        (work / "manifest.json").write_text(json.dumps(manifest, indent=2))

        # ---- Bundle ------------------------------------------------------
        archive = DEST / f"a3-{stamp}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for item in sorted(work.iterdir()):
                tar.add(item, arcname=item.name)

    log(f"wrote {archive} ({archive.stat().st_size // 1024}K)")

    # ---- Rotation --------------------------------------------------------
    # Only ever removes archives this script created, matching its own naming,
    # and only once there are more than KEEP of them.
    archives = sorted(DEST.glob("a3-*.tar.gz"))
    for old in archives[:-KEEP] if len(archives) > KEEP else []:
        log(f"  rotating out {old.name}")
        old.unlink()

    kept = len(list(DEST.glob("a3-*.tar.gz")))
    log(f"done. {kept} backup(s) kept in {DEST}")

    for problem in problems:
        print(f"INCOMPLETE: {problem}", file=sys.stderr)
    # A backup missing a piece must not look like a success to whatever is
    # watching the exit code.
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
