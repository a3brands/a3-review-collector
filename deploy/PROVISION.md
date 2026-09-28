# VERSION 2 — moving the A3 review system off the laptop

The goal is one sentence: **the scrape and the emails keep happening with the
MacBook shut down.**

Nothing in the scraper changes to achieve that. `app/collector/google_maps.py`
is already portable, headless, Linux-ready Python — its Google handling, its
warm-up navigation, its pagination and its deduplication all run unmodified.
The laptop dependency was never in the code. It was in seven macOS launchd
agents and a Postgres server running under a user account.

---

## 0. What is actually wrong today

Seven agents in `~/Library/LaunchAgents/`, all `RunAtLoad`:

| Agent | Holds hostage |
|---|---|
| `com.a3brands.reviewcollector` | the 15-minute scrape loop |
| `com.a3brands.reviewsmanager` | the dashboard and its emails |
| `com.a3brands.reviewtunnel` / `managertunnel` | two cloudflared quick tunnels |
| `com.a3brands.funnel` | re-arms Tailscale Funnel every 300s |
| `com.a3brands.sourcerefresh` | daily Carfax / DealerRater refresh |
| `com.a3brands.backup` | nightly backup |

Plus **Postgres.app running as your user**, holding the entire dashboard
database on the laptop's SSD.

`RunAtLoad` fires **at login**, not at boot. Closing the lid stops all of it,
and launchd does not back-fill missed intervals — a point the `sourcerefresh`
plist already concedes in its own comments.

---

## 1. Choosing a server — the honest version

You asked for free. Here is what "free" actually means in 2026 for a host that
must run headless Chromium every 15 minutes.

| Option | Verdict |
|---|---|
| **Oracle Cloud Always Free (Ampere A1, ARM)** | **Recommended.** Up to 4 vCPU / 24 GB, never expires, no time limit. Genuinely free. |
| Oracle Always Free (AMD e2.micro) | 1 GB RAM. Chromium fits only with swap, and Oracle reclaims idle AMD instances. A1 is exempt. |
| Google Cloud e2-micro free tier | 1 GB RAM. Tight for Chromium; one large listing can OOM it. |
| Fly.io | No longer has a free allowance. |
| Render / Railway free | Sleep or wipe disk on redeploy. Railway is why the dashboard ended up on the laptop in the first place. |
| Hetzner CX22 | **~€4/mo. Not free.** The fallback if Oracle A1 capacity is unobtainable. |

**Three things to know before you start**, none of which are obvious from
Oracle's signup page:

1. **A1 capacity in popular regions is frequently exhausted.** "Out of host
   capacity" is normal and may take several attempts over a few days. Pick a
   less busy home region.
2. **A credit card is required for identity verification** even on Always Free.
   It is not charged, but the account must stay on the Always Free tier — do
   not "upgrade to pay-as-you-go" when prompted.
3. **ARM64 Chromium is the one real technical risk.** Playwright publishes
   arm64 Chromium builds for Ubuntu and they work, but they are less exercised
   than x86. **Step 5 below is a hard gate: do not migrate anything until a
   real scrape succeeds on the actual VM.** If it fails, the answer is an x86
   host, not a rewrite of the scraper.

Provision: **Ubuntu 24.04, Ampere A1, 2 vCPU / 12 GB, 50 GB boot volume.**
Open no inbound ports in the Oracle security list. You will reach the machine
over Tailscale SSH, and both services stay bound to loopback.

---

## 2. Base system

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3.12-venv python3-pip postgresql sqlite3 rsync git curl make

# Node 22, matching the laptop's vendored runtime
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt -y install nodejs
node -v   # expect v22.x

# A service account that cannot be logged into
sudo useradd --system --create-home --home-dir /opt/a3 --shell /usr/sbin/nologin a3
sudo mkdir -p /opt/review-collector /opt/review-responder
sudo chown -R a3:a3 /opt/review-collector /opt/review-responder

sudo timedatectl set-timezone America/Chicago
```

The timezone is not cosmetic. Google publishes relative dates ("2 weeks ago")
and the collector resolves them against the process timezone. A server on UTC
would shift every scraped review date by several hours and re-fingerprint
reviews that have no native Google ID.

---

## 3. Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
sudo systemctl enable tailscaled
```

`--ssh` means you never open port 22 to the internet.

---

## 4. Postgres

```bash
sudo -u postgres createuser a3
sudo -u postgres createdb -O a3 a3_reviews
sudo -u postgres psql -c "ALTER USER a3 WITH PASSWORD 'PICK-A-STRONG-ONE';"
sudo systemctl enable postgresql
```

The laptop runs Postgres.app 16; Ubuntu 24.04 ships 16. Same major version, so
the dump restores cleanly.

---

## 5. THE GATE — prove Chromium scrapes Google from this machine

Do this **before** migrating anything. It is cheap to fail here and expensive
to fail after cutover.

```bash
sudo -u a3 -H bash
cd /opt/review-collector
# (copy just requirements.txt and the app/ tree up by hand, or run
#  deploy/migrate-from-laptop.sh first — it is safe and non-destructive)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium
.venv/bin/python -c "from playwright.sync_api import sync_playwright; \
p=sync_playwright().start(); b=p.chromium.launch(headless=True); \
pg=b.new_page(); pg.goto('https://www.google.com/maps'); print('title:', pg.title()); \
b.close(); p.stop()"
```

Then, once `.env` exists, the real thing:

```bash
.venv/bin/python scripts/collect_once.py --dry-run
```

**Expected:** per-dealership blocks showing `Found: 20`, a `Status: SUCCESS`,
and no email sent.

**If you see `AccessBlocked`,** Google served the reduced listing. Confirm
`PLAYWRIGHT_WARM_SESSION=true` and that `PLAYWRIGHT_USER_AGENT` was copied
across — those two are what took the success rate from 2/8 to 14/14, and they
are the settings most likely to be "cleaned up" during a migration.

**If Chromium will not launch at all on ARM,** stop and move to an x86 host.
Do not modify the scraper.

---

## 6. Migrate

From the **laptop**:

```bash
cd "~/Desktop/PROJECTS/responder separate tool"
./deploy/migrate-from-laptop.sh a3@your-server
```

This copies code and data and **touches nothing on the laptop**. VERSION 1
keeps running throughout.

It deliberately does **not** copy `.env`. Write the two files by hand from
`review-collector/.env.production.example` and
`google-reviews-manager/.env.production.example`, which list exactly which
values change and which secrets to rotate.

> **Why the SQLite copy is done with `sqlite3 .backup` and not `scp`:** the
> database runs in WAL mode. Measured on this laptop today, `reviews.db` alone
> contains **4,869** reviews while the live database has **4,893** — the other
> 24 are in the `-wal` file. A plain copy loses them silently, and the collector
> then re-emails all 24 as new. The script takes a proper snapshot instead.

Then finish on the server, as printed by the script: restore Postgres, build
the venv and `npm ci --omit=dev`, write the `.env` files.

---

## 7. Start the services

```bash
sudo cp /opt/review-collector/deploy/review-collector.service /etc/systemd/system/
sudo cp /opt/review-responder/deploy/review-responder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now review-collector review-responder
sudo systemctl status review-collector --no-pager
```

`enable` is the word that matters. `start` alone works perfectly until the
first reboot and then never again — the same class of bug as `RunAtLoad`.

---

## 8. Publish through Tailscale

```bash
sudo /opt/review-collector/deploy/tailscale-serve.sh
```

| Service | Port | Exposure |
|---|---|---|
| Dashboard | 3999 | **Funnel → public HTTPS** on `https://<server>.ts.net` |
| Collector | 8080 | **Serve → tailnet only** on `https://<server>.ts.net:8443` |
| Collector ← Responder | — | `127.0.0.1`, never leaves the host |

The dashboard must be public: clients open it from phones, and approval links
in emails must resolve from an ordinary inbox. It has its own login.

The collector must not be. Its API can trigger scrapes and read every review,
behind a single shared `A3_API_KEY`. Two **different ports** is what keeps that
true — Funnel is enabled per-port, so funnelling 443 does not funnel 8443.
Serving the collector at a path under 443 would have published it the instant
the dashboard's funnel came up.

Then set `APP_BASE_URL` to the funnel hostname and restart the responder.

> **The hostname changes.** It becomes the server's `*.ts.net` name, not
> `rhealyns-macbook-pro-4`. Approval emails already in inboxes carry the old
> one and will break when the laptop's funnel stops. Send yourself a test
> approval and click it before decommissioning. If that URL is shared with
> clients, point a real domain at the server instead — `deploy/Caddyfile` is
> already written for that.

---

## 9. The tests you specified

```bash
/opt/review-collector/deploy/verify-v2.sh
```

Run it three times:

1. **Laptop still on.** Everything green except possibly FRESH.
2. **Laptop completely shut down, wait 20 minutes.** All green — this is the
   test. It checks that a collection actually happened within the last
   interval, not merely that a scheduler object exists.
3. **`sudo reboot`, wait 20 minutes.** All green, with nobody logged in.

It also checks the two things that fail silently: that `UNIQUE(review_id)`
survived the migration (without it, duplicate emails become possible), and that
exactly one service is funnelled (if the collector is public, it says so in
capital letters).

---

## 10. Decommissioning the laptop — last, and only after test 2 passes

```bash
for a in reviewcollector reviewsmanager reviewtunnel managertunnel funnel sourcerefresh backup; do
  launchctl unload ~/Library/LaunchAgents/com.a3brands.$a.plist
done
```

**Unload, do not delete.** Keep the plists and the laptop's databases until the
server has run unattended for a week. That is VERSION 1 staying intact until
VERSION 2 is proven, and it is the whole rollback plan:

```bash
launchctl load ~/Library/LaunchAgents/com.a3brands.reviewcollector.plist
```

Two things must not run in both places at once:

- **Both collectors scraping.** Not a data-corruption risk — the `UNIQUE`
  constraint holds and dedup is per-database — but both would email, so you
  would get every notification twice.
- **Both funnels.** Whichever came up last wins; the other's links die.

So: stop the laptop's collector at the same moment you enable the server's.

---

## 11. Still on the laptop after this

- `com.a3brands.sourcerefresh` — Carfax/DealerRater via Firecrawl. Moves to the
  server as a systemd timer; it is a daily, non-urgent job and can follow later.
- `com.a3brands.backup` — nightly backup, `scripts/backup.py`. Should be moved
  and pointed at the server's paths.

Neither touches Google, and neither blocks the laptop-off test.
