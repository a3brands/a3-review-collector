# One set of verbs for a system that is two processes in two languages.
#
# The collector is Python and the Responder is Node, which means "run the
# thing" was previously two different commands in two different directories,
# remembered differently by whoever was on the machine. On a server you are
# SSH'd into at an awkward hour, that is how the wrong service gets restarted.
#
#   make help              what each target does
#   make collector-once    one scrape cycle, printed, then exit
#   make status            is any of this actually running?
#
# These are for development and for hands-on work on the server. Production
# does NOT invoke them: systemd runs the two ExecStart lines directly, so a
# broken Makefile can never take the service down.

COLLECTOR := review-collector
RESPONDER := google-reviews-manager
PY        := $(COLLECTOR)/.venv/bin/python
NODE      := node

# node is installed through nvm on the laptop, and nvm is a shell function that
# only exists in an interactive shell. make hands each recipe a bare /bin/sh, so
# every npm line here failed with "npm: command not found" -- including the one
# in `doctor`, which caught that as "Postgres unreachable" and reported a
# healthy database as down. A health check that cannot fail honestly is worse
# than no health check.
#
# Empty on the server, where node is installed normally and this changes
# nothing. Production does not invoke the Makefile either way.
NODE_BIN := $(shell ls -d $(HOME)/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)
ifneq ($(NODE_BIN),)
export PATH := $(NODE_BIN):$(PATH)
endif

.DEFAULT_GOAL := help
.PHONY: help install dev collector collector-once collector-dry db-migrate test logs status doctor

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv, install both apps, install Chromium
	python3 -m venv $(COLLECTOR)/.venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r $(COLLECTOR)/requirements.txt
	$(PY) -m playwright install chromium
	cd $(RESPONDER) && npm ci

dev: ## Run the Responder with reload (dashboard on :3999)
	cd $(RESPONDER) && npm run dev

collector: ## Run the collector in the foreground (API + 15-minute scheduler)
	cd $(COLLECTOR) && .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8080

collector-once: ## One collection cycle, then exit. Stores, emails and syncs.
	cd $(COLLECTOR) && .venv/bin/python scripts/collect_once.py

collector-dry: ## One collection cycle that emails nobody and syncs nowhere
	cd $(COLLECTOR) && .venv/bin/python scripts/collect_once.py --dry-run

db-migrate: ## Apply the Responder's Postgres schema
	cd $(RESPONDER) && npm run db:migrate

test: ## Run both test suites
	cd $(COLLECTOR) && .venv/bin/python -m pytest -q
	cd $(RESPONDER) && npm test

logs: ## Follow both services' logs (server: journald; laptop: log files)
	@if command -v journalctl >/dev/null 2>&1; then \
	  journalctl -u review-collector -u review-responder -f -n 50; \
	else \
	  tail -f $(COLLECTOR)/logs/collector.log "$$HOME/Library/Logs/a3brands/manager.log"; \
	fi

status: ## Is it running, and when did it last collect?
	@echo "--- processes ---"
	@if command -v systemctl >/dev/null 2>&1; then \
	  for u in review-collector review-responder tailscaled postgresql; do \
	    printf '  %-20s %s\n' "$$u" "$$(systemctl is-active $$u 2>/dev/null)"; \
	  done; \
	else \
	  launchctl list 2>/dev/null | grep a3brands || echo "  (no launchd agents loaded)"; \
	fi
	@echo "--- last collection (collector /health) ---"
	@curl -fsS http://127.0.0.1:8080/health 2>/dev/null | head -c 600 \
	  || echo "collector not answering on 127.0.0.1:8080"
	@echo
	@echo "--- dashboard ---"
	@curl -fsS -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:3999/health 2>/dev/null \
	  || echo "responder not answering on 127.0.0.1:3999"

doctor: ## Check the things that silently break: Chromium, DB, SMTP, tailnet
	@echo "--- Chromium ---"
	@$(PY) -c "from playwright.sync_api import sync_playwright; \
p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()" \
	  || echo "MISSING: run  $(PY) -m playwright install chromium"
	@echo "--- Postgres ---"
	@cd $(RESPONDER) && npm run --silent db:migrate || echo "Postgres unreachable"
	@echo "--- SMTP ---"
	@cd $(COLLECTOR) && .venv/bin/python -c "import smtplib,ssl,sys; \
sys.path.insert(0,'.'); \
from app.config import get_settings; \
s=get_settings(); \
problem=s.notify_config_problem(); \
print('config:', problem or 'OK'); \
print('from  :', s.notify_from); \
print('to    :', ', '.join(s.notify_recipients())); \
srv=smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20); \
srv.ehlo(); \
s.smtp_use_tls and (srv.starttls(context=ssl.create_default_context()), srv.ehlo()); \
srv.login(s.smtp_username, s.smtp_password); \
print('auth  : OK -', s.smtp_host, 'accepted the credentials'); \
srv.quit()" \
	  || echo "SMTP BROKEN: notifications are not being delivered"
	@echo "--- Tailscale ---"
	@tailscale status 2>/dev/null | head -3 || echo "tailscale not installed / not up"
	@echo "--- Funnel exposure (only the DASHBOARD should say Funnel) ---"
	@tailscale serve status 2>/dev/null || true
