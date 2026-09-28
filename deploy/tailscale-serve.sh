#!/usr/bin/env bash
# Publish the two services through Tailscale. Run once on the server, as root.
#
#   sudo ./deploy/tailscale-serve.sh
#
# WHAT GOES WHERE, AND WHY THEY ARE NOT THE SAME
#
#   Dashboard  :3999  -> FUNNEL  -> https://<server>.ts.net        PUBLIC
#   Collector  :8080  -> SERVE   -> https://<server>.ts.net:8443   TAILNET ONLY
#
# The dashboard has to be public: clients and staff open it from laptops and
# phones that are not on the tailnet, and the approval links in outgoing emails
# have to resolve from an ordinary inbox. It has its own login and session
# cookies, so it is built to face the internet.
#
# The collector must not be. Its API can trigger scrapes, read every review of
# every dealership and expose backend status, and it is authenticated by a
# single shared A3_API_KEY. Putting that on the public internet to save a step
# would rest the whole security model of this system on one header.
#
# Two DIFFERENT ports is what keeps that true. Funnel is enabled per-port, so
# funnelling 443 does not funnel 8443. Serving the collector under a path on
# 443 -- /collector, say -- would have quietly published it the moment the
# dashboard's funnel came up, which is exactly the mistake this layout avoids.
#
# The Responder does not reach the collector through any of this. They are on
# the same host and talk over 127.0.0.1, so that traffic never touches a
# network interface at all.

set -euo pipefail

DASHBOARD_PORT="${DASHBOARD_PORT:-3999}"
COLLECTOR_PORT="${COLLECTOR_PORT:-8080}"
COLLECTOR_TLS_PORT="${COLLECTOR_TLS_PORT:-8443}"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: tailscale serve/funnel need root." >&2
  exit 1
fi

command -v tailscale >/dev/null || { echo "tailscale is not installed." >&2; exit 1; }

if ! tailscale status >/dev/null 2>&1; then
  echo "This machine is not on a tailnet yet. Run:  sudo tailscale up --ssh" >&2
  exit 1
fi

echo "==> Tailnet identity"
tailscale status --json | grep -o '"DNSName":"[^"]*"' | head -1 || true

# --bg detaches; without it the command holds the terminal and the mapping dies
# with your SSH session, which is a memorable way to lose a dashboard.
echo "==> Dashboard  :${DASHBOARD_PORT} -> public HTTPS (funnel, port 443)"
tailscale funnel --bg --https=443 "http://127.0.0.1:${DASHBOARD_PORT}"

echo "==> Collector  :${COLLECTOR_PORT} -> tailnet-only HTTPS (serve, port ${COLLECTOR_TLS_PORT})"
tailscale serve --bg --https="${COLLECTOR_TLS_PORT}" "http://127.0.0.1:${COLLECTOR_PORT}"

echo
echo "==> Resulting configuration"
tailscale serve status

cat <<'NOTE'

CHECK THIS OUTPUT BEFORE YOU WALK AWAY.

Exactly one line should say "Funnel on". If the collector's 8443 mapping also
says Funnel, the collector is on the public internet -- turn it off with:

    sudo tailscale funnel --https=8443 off

Set APP_BASE_URL in /opt/review-responder/.env to the https://<server>.ts.net
address printed above, then:  sudo systemctl restart review-responder

Approval emails already in flight carry the OLD hostname and stop working once
the laptop's funnel is switched off. Send yourself a test approval and click the
button before decommissioning the laptop.
NOTE
