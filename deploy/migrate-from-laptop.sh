#!/usr/bin/env bash
# Copy code and data from the laptop to the server. Run ON THE LAPTOP.
#
#   ./deploy/migrate-from-laptop.sh a3@review-server
#
# Safe to run repeatedly. It reads from the laptop and writes to the server, and
# never deletes anything on either -- so you can run it once to seed the server,
# work on it for a week, and run it again at cutover to bring the data current.
#
# WHAT IT WILL NOT DO
#   - stop the laptop's services       (cutover is a separate, deliberate step)
#   - copy .env                        (secrets are rotated by hand; see the
#                                       two .env.production.example files)
#   - delete anything                  (VERSION 1 stays intact and runnable
#                                       until you have proved VERSION 2)

set -euo pipefail

TARGET="${1:-}"
[[ -n "$TARGET" ]] || { echo "usage: $0 user@server" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COLLECTOR="$ROOT/review-collector"
RESPONDER="$ROOT/google-reviews-manager"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

PG_BIN="${PG_BIN:-/Applications/Postgres.app/Contents/Versions/16/bin}"

say() { printf '\n==> %s\n' "$*"; }

# ---------------------------------------------------------------------------
say "Checking the server is reachable"
ssh -o ConnectTimeout=10 "$TARGET" 'echo "connected to $(hostname)"'
ssh "$TARGET" 'sudo mkdir -p /opt/review-collector /opt/review-responder && sudo chown -R $(id -un):$(id -gn) /opt/review-collector /opt/review-responder'

# ---------------------------------------------------------------------------
say "Collector SQLite -> consistent snapshot"
# NOT a plain copy. The database runs in WAL mode, and right now the -wal file
# holds several megabytes of reviews that are not in reviews.db yet: the main
# file's mtime can be days behind. scp'ing reviews.db on its own silently loses
# every review since the last checkpoint -- and the damage is invisible, because
# the server starts fine, shows fewer reviews, and re-emails the missing ones as
# new the first time it scrapes.
#
# `.backup` asks SQLite itself for a consistent snapshot including the WAL,
# while the collector is still running and writing. That is the whole reason to
# use it rather than stopping the service first.
sqlite3 "$COLLECTOR/data/reviews.db" ".backup '$STAGE/reviews.db'"
sqlite3 "$STAGE/reviews.db" 'PRAGMA integrity_check;' | head -1
COUNT=$(sqlite3 "$STAGE/reviews.db" 'SELECT COUNT(*) FROM reviews;')
echo "snapshot contains $COUNT reviews"

# ---------------------------------------------------------------------------
say "Responder Postgres -> dump"
[[ -x "$PG_BIN/pg_dump" ]] || { echo "pg_dump not found at $PG_BIN (set PG_BIN=)" >&2; exit 1; }
# --no-owner / --no-acl: the laptop's role names are not the server's, and
# without these the restore fails on every GRANT for a user that does not exist.
"$PG_BIN/pg_dump" --no-owner --no-acl --clean --if-exists \
  "${DATABASE_URL:-postgres://127.0.0.1:5432/a3_reviews}" > "$STAGE/a3_reviews.sql"
echo "dump is $(wc -c < "$STAGE/a3_reviews.sql") bytes"

# ---------------------------------------------------------------------------
say "Code -> server"
# Excludes are the point here. node_modules and .venv are platform-specific
# (the laptop's are arm64 macOS binaries) and must be rebuilt on the server, not
# copied. .env is excluded because those secrets are being rotated, not moved.
RSYNC_EXCLUDES=(
  --exclude '.git' --exclude 'node_modules' --exclude '.venv' --exclude '.node'
  --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.DS_Store'
  --exclude '.env' --exclude '.env.bak*' --exclude 'logs/*' --exclude 'data'
)
rsync -az --info=stats1 "${RSYNC_EXCLUDES[@]}" "$COLLECTOR/" "$TARGET:/opt/review-collector/"
rsync -az --info=stats1 "${RSYNC_EXCLUDES[@]}" "$RESPONDER/" "$TARGET:/opt/review-responder/"

# ---------------------------------------------------------------------------
say "Data -> server"
ssh "$TARGET" 'mkdir -p /opt/review-collector/data /opt/review-collector/logs /opt/review-responder/data/reviews'
scp -q "$STAGE/reviews.db"     "$TARGET:/opt/review-collector/data/reviews.db"
scp -q "$STAGE/a3_reviews.sql" "$TARGET:/tmp/a3_reviews.sql"
# The Carfax and DealerRater pulls, plus accounts.json and dealers.json, which
# are still files rather than Postgres rows.
rsync -az --info=stats1 "$RESPONDER/data/" "$TARGET:/opt/review-responder/data/"

# ---------------------------------------------------------------------------
say "Done copying. Remaining steps are on the SERVER:"
cat <<NEXT

  # 1. restore Postgres
  sudo -u postgres createdb a3_reviews 2>/dev/null || true
  psql "postgres://a3@127.0.0.1:5432/a3_reviews" < /tmp/a3_reviews.sql
  rm /tmp/a3_reviews.sql          # it contains customer data

  # 2. build the two runtimes for THIS machine
  cd /opt/review-collector && python3 -m venv .venv \\
    && .venv/bin/pip install -r requirements.txt \\
    && .venv/bin/playwright install --with-deps chromium
  cd /opt/review-responder && npm ci --omit=dev

  # 3. write the two .env files (see each project's .env.production.example)

  # 4. prove the scrape works on this machine BEFORE enabling anything
  cd /opt/review-collector && .venv/bin/python scripts/collect_once.py --dry-run

  # 5. only then:  sudo systemctl enable --now review-collector review-responder

The laptop has not been touched. VERSION 1 is still running and still correct.
NEXT
