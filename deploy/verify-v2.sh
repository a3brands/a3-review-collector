#!/usr/bin/env bash
# The VERSION 2 acceptance test. Run ON THE SERVER.
#
#   ./deploy/verify-v2.sh
#
# This is the script for the laptop-off test and the reboot test. It answers one
# question -- "would this still be working if the laptop were in a drawer?" --
# and it answers it by checking the things that actually break, not by checking
# that files exist.
#
# It is read-only. It starts nothing, stops nothing and sends no email.
#
# Run it three times:
#   1. after provisioning, laptop still on   -> everything green except FRESH
#   2. laptop shut down, 20 minutes later    -> everything green, FRESH green
#   3. after `sudo reboot`, 20 minutes later -> everything green, FRESH green
#
# If run 2 is green, VERSION 2 is complete. If it is not, it tells you which
# link in the chain is missing.

set -uo pipefail

PASS=0; FAIL=0; WARN=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$*"; WARN=$((WARN+1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$*"; }

COLLECTOR_URL="${COLLECTOR_URL:-http://127.0.0.1:8080}"
DASHBOARD_URL="${DASHBOARD_URL:-http://127.0.0.1:3999}"

# ---------------------------------------------------------------------------
head_ "1. Services survive without a logged-in user"
for unit in review-collector review-responder tailscaled postgresql; do
  state=$(systemctl is-active "$unit" 2>/dev/null || echo unknown)
  enabled=$(systemctl is-enabled "$unit" 2>/dev/null || echo unknown)
  if [[ "$state" == active ]]; then ok "$unit is running"; else bad "$unit is $state"; fi
  # enabled is the one that matters for the reboot test. A unit that is active
  # but not enabled works perfectly until the first reboot and then never again.
  if [[ "$enabled" == enabled || "$enabled" == enabled-runtime ]]; then
    ok "$unit starts at boot"
  else
    bad "$unit is NOT enabled -- it will not come back after a reboot"
  fi
done

head_ "2. Nothing is waiting on a human session"
if loginctl list-sessions --no-legend 2>/dev/null | grep -q .; then
  warn "a user session is open (fine now; the test is that it is not REQUIRED)"
else
  ok "no interactive session -- services are running headless"
fi

# ---------------------------------------------------------------------------
head_ "3. Database"
if sudo -u postgres psql -tAc 'SELECT 1' >/dev/null 2>&1; then
  ok "Postgres accepting connections"
else
  bad "Postgres not answering"
fi
DB=/opt/review-collector/data/reviews.db
if [[ -f "$DB" ]]; then
  n=$(sqlite3 "$DB" 'SELECT COUNT(*) FROM reviews;' 2>/dev/null || echo 0)
  b=$(sqlite3 "$DB" 'SELECT COUNT(*) FROM businesses WHERE active=1;' 2>/dev/null || echo 0)
  [[ "$n" -gt 0 ]] && ok "collector SQLite holds $n reviews across $b active dealerships" \
                   || bad "collector SQLite is empty -- did the migration copy the WAL?"
  # The unique index is what makes a duplicate email impossible. Losing it in a
  # migration is silent until the day the same review is emailed twice.
  if sqlite3 "$DB" '.indexes reviews' 2>/dev/null | grep -q uq_reviews_review_id; then
    ok "UNIQUE(review_id) intact -- duplicates cannot be inserted"
  else
    bad "UNIQUE(review_id) is MISSING -- duplicate reviews and repeat emails are possible"
  fi
else
  bad "collector database not found at $DB"
fi

# ---------------------------------------------------------------------------
head_ "4. Collector"
health=$(curl -fsS --max-time 10 "$COLLECTOR_URL/health" 2>/dev/null || echo '')
if [[ -z "$health" ]]; then
  bad "collector /health did not answer on $COLLECTOR_URL"
else
  ok "collector API answering"
  grep -q '"scheduler_running":true' <<<"$health" \
    && ok "15-minute scheduler is running" \
    || bad "scheduler is NOT running -- nothing will be collected"
  interval=$(grep -o '"check_interval_minutes":[0-9]*' <<<"$health" | cut -d: -f2)
  [[ "${interval:-0}" -le 15 ]] && ok "check interval is ${interval} minute(s)" \
                                || warn "check interval is ${interval:-?} minutes"
  echo "        $health" | head -c 400; echo
fi

head_ "5. FRESH -- has it collected recently, on its own?"
# The heart of the laptop-off test. Everything above can be green on a server
# that has not actually scraped anything since you last poked it by hand.
last=$(grep -o '"last_successful_check":"[^"]*"' <<<"$health" | cut -d'"' -f4)
if [[ -z "$last" ]]; then
  bad "no successful check on record"
else
  age=$(( $(date -u +%s) - $(date -u -d "${last%Z}" +%s 2>/dev/null || echo 0) ))
  if   (( age < 0 ));    then warn "last check timestamp is in the future -- check the clock"
  elif (( age < 1200 )); then ok "last successful check was $((age/60))m ago -- collecting unattended"
  elif (( age < 3600 )); then warn "last successful check was $((age/60))m ago -- longer than one interval"
  else                        bad "last successful check was $((age/3600))h ago -- collection has stopped"
  fi
fi

# ---------------------------------------------------------------------------
head_ "6. Dashboard"
code=$(curl -fsS -o /dev/null -w '%{http_code}' --max-time 10 "$DASHBOARD_URL/health" 2>/dev/null || echo 000)
[[ "$code" == 200 ]] && ok "dashboard answering (HTTP $code)" || bad "dashboard returned HTTP $code"

# ---------------------------------------------------------------------------
head_ "7. Tailscale -- and what is exposed"
if tailscale status >/dev/null 2>&1; then
  ok "tailnet is up as $(tailscale status --json | grep -o '"DNSName":"[^"]*"' | head -1 | cut -d'"' -f4)"
  serve=$(tailscale serve status 2>/dev/null || echo '')
  funnels=$(grep -c 'Funnel on' <<<"$serve" || true)
  if   (( funnels == 1 )); then ok "exactly one service is public (the dashboard)"
  elif (( funnels == 0 )); then bad "nothing is funnelled -- the dashboard is not reachable publicly"
  else                          bad "$funnels services are PUBLIC -- the collector may be exposed"
  fi
  grep -q '8443' <<<"$serve" && ok "collector served on 8443 (tailnet only)" \
                             || warn "collector is not published on the tailnet"
  # An explicit check, because this is the failure that is invisible until abused.
  if grep -A2 '8443' <<<"$serve" | grep -q 'Funnel on'; then
    bad "THE COLLECTOR IS ON THE PUBLIC INTERNET -- sudo tailscale funnel --https=8443 off"
  fi
else
  bad "tailscale is not up"
fi

# ---------------------------------------------------------------------------
head_ "8. Email path"
smtp_host=$(grep -E '^SMTP_HOST=' /opt/review-collector/.env 2>/dev/null | cut -d= -f2-)
smtp_port=$(grep -E '^SMTP_PORT=' /opt/review-collector/.env 2>/dev/null | cut -d= -f2-)
if [[ -n "${smtp_host:-}" ]]; then
  if timeout 8 bash -c "</dev/tcp/${smtp_host}/${smtp_port:-587}" 2>/dev/null; then
    ok "outbound SMTP to ${smtp_host}:${smtp_port:-587} is open"
  else
    bad "cannot reach ${smtp_host}:${smtp_port:-587} -- the provider may block outbound SMTP"
  fi
  grep -qE '^NOTIFY_ENABLED=true' /opt/review-collector/.env \
    && ok "notifications enabled" || warn "NOTIFY_ENABLED is not true -- no emails will be sent"
else
  bad "SMTP_HOST not set in /opt/review-collector/.env"
fi
# Proof the send path ran, not just that the port is open.
emailed=$(sqlite3 "$DB" "SELECT COUNT(*) FROM reviews WHERE notified_at IS NOT NULL;" 2>/dev/null || echo 0)
[[ "$emailed" -gt 0 ]] && ok "$emailed review(s) have been emailed from this database" \
                       || warn "no review has been emailed yet (expected on a fresh server)"

# ---------------------------------------------------------------------------
head_ "9. Secrets are not world-readable"
for f in /opt/review-collector/.env /opt/review-responder/.env; do
  if [[ -f "$f" ]]; then
    perm=$(stat -c '%a' "$f" 2>/dev/null || stat -f '%Lp' "$f")
    [[ "$perm" == 600 || "$perm" == 400 ]] && ok "$f is $perm" || bad "$f is $perm -- chmod 600"
  else
    bad "$f is missing"
  fi
done

# ---------------------------------------------------------------------------
head_ "10. No leftover laptop dependency"
if grep -rlE '/Users/[a-z]+/Desktop|trycloudflare|launchctl' \
     /opt/review-collector/.env /opt/review-responder/.env 2>/dev/null | grep -q .; then
  bad "a laptop path or cloudflare tunnel is still referenced in a .env"
else
  ok "no laptop paths or quick-tunnel hostnames in the environment files"
fi

# ---------------------------------------------------------------------------
printf '\n\033[1mRESULT\033[0m  %d passed, %d failed, %d warnings\n' "$PASS" "$FAIL" "$WARN"
if (( FAIL == 0 )); then
  echo "VERSION 2 acceptance: PASS. Re-run this with the laptop shut down to confirm."
  exit 0
fi
echo "VERSION 2 acceptance: FAIL. The laptop cannot be decommissioned yet."
exit 1
