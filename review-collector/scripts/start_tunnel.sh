#!/usr/bin/env bash
# Publish the local dashboard on a public HTTPS URL via a Cloudflare quick tunnel.
#
# Quick tunnels need no Cloudflare account and cost nothing, but the hostname is
# assigned fresh on every start. So the URL is written to data/public_url.txt and
# the collector reads it when composing an email -- links are always current
# without anyone editing .env.
set -euo pipefail
cd "$(dirname "$0")/.."

CLOUDFLARED="${CLOUDFLARED:-$HOME/.local/bin/cloudflared}"
PORT="$(grep -E '^PORT=' .env | cut -d= -f2- || echo 8080)"
LOG="logs/tunnel.log"
URL_FILE="data/public_url.txt"

mkdir -p logs data
: > "$LOG"

"$CLOUDFLARED" tunnel --no-autoupdate --url "http://127.0.0.1:${PORT}" >> "$LOG" 2>&1 &
TUNNEL_PID=$!
# Clean up the recorded URL if the tunnel dies, so emails fall back to localhost
# rather than advertising a hostname that no longer resolves.
trap 'rm -f "$URL_FILE"; kill $TUNNEL_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)"
  if [ -n "${URL:-}" ]; then
    printf '%s' "$URL" > "$URL_FILE"
    echo "Public dashboard: $URL"
    echo "(written to $URL_FILE - emails will use it automatically)"
    wait $TUNNEL_PID
    exit $?
  fi
  sleep 1
done

echo "Tunnel did not report a URL within 40s. Last lines of $LOG:" >&2
tail -5 "$LOG" >&2
exit 1
