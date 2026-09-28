#!/usr/bin/env python3
"""Run every test in both projects, and say plainly whether it is safe to ship.

    python3 scripts/check.py            # both suites
    python3 scripts/check.py --quick    # skip the slow route smoke tests

Two suites live in two languages in two directories, so "did I break anything"
used to mean remembering both. It did not get remembered: two tests were red for
long enough that nobody noticed, and three regressions shipped past a green run
of the other suite.

Written in Python and run by the collector's interpreter for the same reason as
backup.py: macOS will not let a LaunchAgent read anything under ~/Desktop unless
the binary doing the reading also lives there.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("ROOT") or Path(__file__).resolve().parent.parent)
RESPONDER = ROOT / "google-reviews-manager"
COLLECTOR = ROOT / "review-collector"

QUICK = "--quick" in sys.argv


def run(label: str, command: list[str], cwd: Path, env: dict | None = None) -> tuple[bool, str]:
    started = time.monotonic()
    print(f"\n{'=' * 62}\n{label}\n{'=' * 62}")
    try:
        done = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True,
            env={**os.environ, **(env or {})}, timeout=600,
        )
    except FileNotFoundError:
        return False, f"{label}: could not run {command[0]}"
    except subprocess.TimeoutExpired:
        return False, f"{label}: timed out after 10 minutes"

    output = (done.stdout or "") + (done.stderr or "")
    # Only the summary matters when everything passes; the whole log matters
    # when something does not.
    if done.returncode == 0:
        for line in output.splitlines():
            if any(k in line for k in ("Tests:", "Test Suites:", "passed", "skipped")):
                print("  " + line.strip())
    else:
        print(output[-4000:])

    took = time.monotonic() - started
    print(f"  ({took:.0f}s)")
    return done.returncode == 0, label


def main() -> int:
    results = []

    node = RESPONDER / ".node" / "bin" / "node"
    npm = RESPONDER / ".node" / "bin" / "npm"
    jest_env = {"PATH": f"{RESPONDER / '.node' / 'bin'}:{os.environ.get('PATH', '')}"}

    if npm.exists():
        args = [str(npm), "test"]
        if QUICK:
            # The smoke suite boots a real server, so it is the slow one.
            args += ["--", "--testPathIgnorePatterns", "routes.smoke"]
        ok, label = run("Responder (Node)", args, RESPONDER, jest_env)
        results.append((ok, label))
    else:
        results.append((False, "Responder (Node): .node/bin/npm not found"))

    python = COLLECTOR / ".venv" / "bin" / "python"
    if python.exists():
        ok, label = run(
            "Collector (Python)",
            [str(python), "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
            COLLECTOR,
        )
        results.append((ok, label))
    else:
        results.append((False, "Collector (Python): .venv/bin/python not found"))

    print(f"\n{'=' * 62}")
    failed = [label for ok, label in results if not ok]
    for ok, label in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")

    if failed:
        print("\nNot safe to ship. Fix the above first.")
        return 1
    print("\nBoth suites green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
