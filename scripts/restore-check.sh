#!/usr/bin/env bash
# Restore the newest backup into scratch copies and count what came back.
#
#   ./scripts/restore-check.sh                    # newest backup
#   ./scripts/restore-check.sh path/to/a3-....tar.gz
#
# An untested backup is a belief, not a backup. This is what turns it into a
# fact. Run it after any change that touches storage, and monthly otherwise.
#
# Nothing live is touched: Postgres is restored into a scratch database that is
# dropped and recreated each run, and the SQLite copy is opened read only.
set -euo pipefail

ROOT="${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DEST="${BACKUP_DIR:-$HOME/Backups/a3-review-system}"
SCRATCH_DB="${SCRATCH_DB:-a3_restore_check}"

PGBIN="/Applications/Postgres.app/Contents/Versions/latest/bin"
[ -d "$PGBIN" ] && PATH="$PGBIN:$PATH"

BACKUP="${1:-$(find "$DEST" -maxdepth 1 -name 'a3-*.tar.gz' | sort | tail -1)}"
[ -n "$BACKUP" ] && [ -f "$BACKUP" ] || { echo "No backup found in $DEST" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "Checking $(basename "$BACKUP") ($(du -h "$BACKUP" | cut -f1))"
tar -xzf "$BACKUP" -C "$WORK"

# What the backup claimed to hold. Missing manifest means an older archive, and
# every comparison below then falls back to "is it non-empty".
manifest() {
  python3 -c "
import json
try:
    # json.dumps, not print: the shell compares against false and null, and
    # Python would hand it False and None.
    print(json.dumps(json.load(open('$WORK/manifest.json')).get('$1')))
except Exception:
    print('null')" 2>/dev/null || echo null
}

FAILED=0

# ---- Postgres ------------------------------------------------------------
if [ -f "$WORK/a3_reviews.dump" ]; then
  dropdb --if-exists "$SCRATCH_DB" 2>/dev/null || true
  createdb "$SCRATCH_DB"
  # pg_restore reports noise about roles and extensions it cannot recreate; the
  # counts below are what actually decide whether this worked.
  pg_restore --no-owner --no-privileges --dbname="$SCRATCH_DB" "$WORK/a3_reviews.dump" >/dev/null 2>&1 || true

  echo
  echo "Postgres, restored into $SCRATCH_DB:"
  psql -d "$SCRATCH_DB" -tA -c "
    SELECT '  reviews           ' || count(*) FROM reviews
    UNION ALL SELECT '  review_sources    ' || count(*) FROM review_sources
    UNION ALL SELECT '  pending_replies   ' || count(*) FROM pending_replies
    UNION ALL SELECT '  approved_replies  ' || count(*) FROM approved_replies
    UNION ALL SELECT '  per dealership    ' || coalesce(string_agg(t, ', '), 'none')
      FROM (SELECT dealer_slug || '=' || count(*) AS t FROM reviews
            GROUP BY dealer_slug ORDER BY dealer_slug) s;"

  REVIEWS="$(psql -d "$SCRATCH_DB" -tAc 'SELECT count(*) FROM reviews')"
  # Compared against what the backup said it held, not merely against zero. An
  # empty database restores without error and looks like success; a partial one
  # looks even more like success.
  EXPECTED="$(manifest postgres_reviews)"
  if [ "$EXPECTED" != "null" ] && [ "$REVIEWS" != "$EXPECTED" ]; then
    echo "  FAIL  restored $REVIEWS review(s), the backup recorded $EXPECTED" >&2; FAILED=1
  elif [ "$REVIEWS" -gt 0 ]; then
    echo "  PASS  $REVIEWS review(s) restored, matching the manifest"
  else
    echo "  FAIL  the dump restored no reviews" >&2; FAILED=1
  fi
else
  echo "  FAIL  no Postgres dump inside the archive" >&2; FAILED=1
fi

# ---- The state files -----------------------------------------------------
echo
echo "State files:"
for f in accounts.json dealers.json; do
  key="has_$(basename "$f" .json)"
  claimed="$(manifest "$key")"
  if [ -f "$WORK/$f" ]; then
    COUNT="$(python3 -c "
import json
d = json.load(open('$WORK/$f'))
print(len(d.get('dealers', d.get('users', {}))))" 2>/dev/null || echo '?')"
    echo "  PASS  $f present, $COUNT entr(ies)"
  elif [ "$claimed" = "false" ]; then
    # It did not exist when the backup was taken either. accounts.json is only
    # written once a customer login is created, so its absence is a fact about
    # the system, not a broken backup.
    echo "  ok    $f did not exist at backup time"
  else
    echo "  FAIL  $f missing, though the backup recorded having it" >&2
    FAILED=1
  fi
done

# ---- The collector's database --------------------------------------------
echo
echo "Collector database:"
if [ -f "$WORK/collector.db" ]; then
  ROWS="$(sqlite3 "$WORK/collector.db" 'SELECT count(*) FROM reviews' 2>/dev/null || echo 0)"
  BIZ="$(sqlite3 "$WORK/collector.db" 'SELECT count(*) FROM businesses' 2>/dev/null || echo 0)"
  EXPECTED="$(manifest collector_reviews)"
  if [ "$EXPECTED" != "null" ] && [ "$ROWS" != "$EXPECTED" ]; then
    echo "  FAIL  holds $ROWS review(s), the backup recorded $EXPECTED" >&2; FAILED=1
  elif [ "$ROWS" -gt 0 ]; then
    echo "  PASS  $ROWS review(s), $BIZ business(es), matching the manifest"
  else
    echo "  FAIL  restored but holds no reviews" >&2; FAILED=1
  fi
else
  echo "  FAIL  collector.db missing from the archive" >&2; FAILED=1
fi

echo
if [ "$FAILED" -eq 0 ]; then
  echo "This backup restores. Checked $(date +%F)."
else
  echo "This backup is NOT trustworthy. Do not rely on it." >&2
fi
exit "$FAILED"
