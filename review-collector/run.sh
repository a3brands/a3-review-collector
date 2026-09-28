#!/usr/bin/env bash
# Start the A3 Review Collector (API + dashboard + automatic scheduler).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "No .venv found. Run:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
if [ ! -f .env ]; then
  echo "No .env found. Run:  cp .env.example .env   (then set A3_API_KEY)"
  exit 1
fi

HOST="$(grep -E '^HOST=' .env | cut -d= -f2- || true)"
PORT="$(grep -E '^PORT=' .env | cut -d= -f2- || true)"
exec .venv/bin/python -m uvicorn app.main:app \
  --host "${HOST:-127.0.0.1}" --port "${PORT:-8080}"
