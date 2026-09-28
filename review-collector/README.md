# A3 Review Collector

An independent service that watches the Google Business Profiles of

- **BMW of Fort Walton Beach** — 1006 Beal Pkwy NW, Fort Walton Beach, FL 32547
- **Mercedes-Benz of Fort Walton Beach** — 1000 Beal Pkwy NW, Fort Walton Beach, FL 32547

finds new reviews, stores them in SQLite, and hands them to your existing
**A3 Review Responder** over a small REST API.

It does **not** generate responses and contains no AI model. It only does:

```
Find -> Collect -> Store -> Provide to A3
```

Your A3 Review Responder is untouched. It talks to this service through two HTTP
calls, so you can replace or rewrite this collector later without changing A3.

```
BMW of Fort Walton Beach GBP ─┐
                              ├─> Review Collector ─> SQLite ─> REST API ─> A3 Review Responder ─> AI response
Mercedes-Benz of FWB GBP ─────┘
```

---

## Table of contents

1. [Google access reality check](#1-google-access-reality-check) ← **read this first**
2. [What you get](#2-what-you-get)
3. [Installation](#3-installation)
4. [Configuration](#4-configuration)
5. [Choosing a collection backend](#5-choosing-a-collection-backend)
6. [Running it](#6-running-it)
7. [Keeping it running automatically](#7-keeping-it-running-automatically)
8. [A3 integration](#8-a3-integration)
9. [API reference](#9-api-reference)
10. [Dashboard](#10-dashboard)
11. [Testing](#11-testing)
12. [Deployment options and free-tier limits](#12-deployment-options-and-free-tier-limits)
13. [Troubleshooting](#13-troubleshooting)
14. [How it works internally](#14-how-it-works-internally)
15. [Email notifications](#15-email-notifications)

---

## 1. Google access reality check

**Please read this before anything else. It is the single most important thing
to understand about this project, and it is the honest result of testing against
your two actual listings — not an assumption.**

You asked for $0 recurring cost and no paid scraping services. That constraint is
met. But Google has made anonymous review collection unreliable, and you should
know exactly where you stand.

### What was tested (2026-08-19, against your two real Place IDs)

| Approach | Result |
|---|---|
| Google Maps place page in headless Chromium | Business data loads fine. Reviews **often** stripped: Google shows *"You're seeing a limited view of Google Maps"* with only Overview/About tabs. |
| Same page in **headful** (visible) Chromium | Identical. So this is **not** headless detection. |
| Same page with a cookie-consent cookie set | Identical. |
| Google's internal `listugcposts` review RPC | **HTTP 403 Forbidden** without a signed-in session. |
| `google.com/search?q=...` and `search.google.com/local/reviews` | Immediately redirected to a **CAPTCHA** (`/sorry/`). |
| Google Business Profile API (official) | **Works, free, complete, reliable.** |

### What this means

Google serves signed-out clients one of two layouts, seemingly at random per
session:

- **Full listing** — a "Reviews for &lt;business&gt;" tab, a sort control, and a
  lazy-loading review list. The scraper works properly here. **During testing it
  really did collect reviews this way**: 5 genuine reviews for Mercedes-Benz of
  Fort Walton Beach, with Google's own permanent review IDs, real reviewer names,
  real star ratings and real review text.
- **Reduced listing** — Overview/About only, no reviews at all. In later test
  runs this was served on 6 consecutive attempts for both dealerships.

So the `playwright` backend **genuinely works sometimes and genuinely fails
other times.** The retry logic gives each check several independent browser
sessions, which materially improves the odds, but it is still chance.

Getting past the reduced listing would require signing in, solving CAPTCHAs, or
spoofing browser fingerprints. You told me not to do any of those, and this
project does not. When Google blocks it, the check is recorded as **FAILED**,
the dashboard shows the error, `/health` reports `degraded`, and **no fake
reviews are ever invented**.

### The recommendation

Use the **Google Business Profile API** (`COLLECTOR_BACKEND=gbp_api`).

- It is genuinely free. Google's own pricing page: *"The Google My Business API
  is available to registered users at no charge."* No billing account, no
  per-call cost, no credit card.
- It is supported and stable — it will not break when Google restyles Maps.
- It returns **complete** data with permanent `reviewId` values, which makes
  de-duplication exact instead of best-effort.
- It gives **exact** review timestamps. The public pages only expose relative
  dates ("2 weeks ago"), so the scraper's dates are approximate — and this
  service flags them honestly as `review_date_is_approximate: true`.
- You qualify: A3 manages these dealerships' profiles.

The one-time cost is a free access-approval form (see
[section 5](#5-choosing-a-collection-backend)).

**Bottom line:** keep `playwright` as a zero-setup fallback that works when
Google lets it. Set up `gbp_api` for collection you can actually rely on. Both
are $0.

---

## 2. What you get

- Automatic checks every 15 minutes (configurable), started with the app
- Independent per-dealership processing — one failing never stops the other
- SQLite storage that survives restarts, with a `UNIQUE` constraint that makes
  duplicate reviews impossible
- REST API for A3, protected by a Bearer API key
- Admin dashboard with a **Check Now** button
- Public `/health` endpoint that reports honestly when collection is failing
- Pluggable collection layer — swap the Google code without touching anything else
- 80 automated tests

---

## 3. Installation

Requires **Python 3.10+** (3.11 recommended). Everything below is free.

```bash
cd review-collector

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

pip install -r requirements.txt

# Only needed for the playwright backend (~150 MB, one time)
python -m playwright install chromium
```

Then create your config:

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # paste into A3_API_KEY
```

---

## 4. Configuration

Everything lives in `.env`. **Never commit it** — `.gitignore` already excludes
it. `.env.example` is the documented template.

The two dealerships come pre-filled with Place IDs verified against Google Maps
on 2026-08-19:

```env
BMW_FWB_PLACE_ID=ChIJ0XFsWkI-kYgR1j9GfmxgynY   # 1006 Beal Pkwy NW, Fort Walton Beach, FL
MB_FWB_PLACE_ID=ChIJ8SSirEM-kYgRPatxgc1nH88    # 1000 Beal Pkwy NW, Fort Walton Beach, FL
```

These are Fort Walton **Beach**, Florida — not Fort Washington. To confirm them
yourself, or to look up a different business:

```bash
python scripts/find_place_id.py "BMW of Fort Walton Beach"
```

It prints each candidate's street address so you can verify before pasting.
The collector also checks the listing's heading at collection time and refuses
to store reviews from a business whose name does not match.

### Settings you are most likely to change

| Variable | Default | Meaning |
|---|---|---|
| `A3_API_KEY` | *(empty)* | **Required.** Bearer token for the API. Empty = every request rejected with 503. |
| `CHECK_INTERVAL_MINUTES` | `15` | How often both dealerships are checked. |
| `COLLECTOR_BACKEND` | `auto` | `gbp_api`, `playwright`, or `auto`. |
| `REVIEWS_PER_CHECK` | `20` | Newest N reviews fetched per ordinary check. |
| `INITIAL_SYNC` | `true` | Pull the back-catalogue on a business's first check. |
| `INITIAL_SYNC_MARK_PROCESSED` | `true` | Store history as already-processed so A3 is not flooded. |
| `PORT` | `8080` | HTTP port. |
| `DATABASE_URL` | `sqlite:///./data/reviews.db` | Use an **absolute** path in production. |

---

## 5. Choosing a collection backend

### Option A — Google Business Profile API (recommended, free)

One-time setup, roughly 20 minutes plus Google's approval wait.

1. Create a project at <https://console.cloud.google.com/>.
2. Enable these three APIs on it:
   - Google My Business API
   - My Business Account Management API
   - My Business Business Information API
3. Request API access (free) via the form linked from
   <https://developers.google.com/my-business/content/prereqs>.
   Use the Google account that **manages** the two dealership profiles.
   You are approved when the quota in Cloud Console reads **300 QPM** instead of 0.
   This typically takes a few days.
4. Create an **OAuth 2.0 Client ID** of type *Desktop app*. Put the client ID and
   secret into `.env`.
5. Get a refresh token:
   ```bash
   python scripts/gbp_authorize.py
   ```
   Sign in as the managing account and paste the printed
   `GOOGLE_REFRESH_TOKEN` into `.env`.
6. Find the location IDs:
   ```bash
   python scripts/gbp_list_locations.py
   ```
   Paste the matching values:
   ```env
   BMW_FWB_GBP_LOCATION=accounts/123456789/locations/987654321
   MB_FWB_GBP_LOCATION=accounts/123456789/locations/123123123
   COLLECTOR_BACKEND=gbp_api
   ```

**Cost: $0.** No billing account is required.

### Option B — Public Google Maps (zero setup, best effort)

```env
COLLECTOR_BACKEND=playwright
```

No credentials. Works when Google serves the full listing, fails clearly when it
does not. Check where you stand:

```bash
python scripts/diagnose_maps.py
```

It reports, per dealership, whether reviews are reachable from your network and
exits non-zero if they are not.

### Option C — `auto` (default)

Uses `gbp_api` when OAuth credentials are present, otherwise `playwright`. With
credentials configured it tries the API first and falls back to the browser.

---

## 6. Running it

```bash
source .venv/bin/activate
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Or use the helper:

```bash
./run.sh
```

Starting the app starts everything — database, API, dashboard and the automatic
scheduler. There is nothing else to run and no scraper to trigger by hand.

- Dashboard — <http://localhost:8080/>
- Health — <http://localhost:8080/health>
- API docs — <http://localhost:8080/docs>

### Lifecycle controls

| Action | How |
|---|---|
| Start | `./run.sh`, or start the service (section 7) |
| Stop | `Ctrl-C`, or stop the service |
| Restart scheduler | `POST /api/admin/scheduler/restart` |
| Stop scheduler only | `POST /api/admin/scheduler/stop` |
| Health check | `GET /health` |
| Manual check | Dashboard **Check Now**, or `POST /api/admin/check-now` |

---

## 7. Keeping it running automatically

### macOS (launchd) — recommended for running on your own Mac

A ready-made agent is included. Edit the paths inside it, then:

```bash
cp com.a3brands.reviewcollector.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.a3brands.reviewcollector.plist
launchctl start com.a3brands.reviewcollector
```

It starts at login and restarts automatically if it ever exits.

```bash
launchctl unload ~/Library/LaunchAgents/com.a3brands.reviewcollector.plist   # stop
```

### Linux (systemd)

`deploy/review-collector.service` is included. Edit the paths, then:

```bash
sudo cp deploy/review-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now review-collector
sudo journalctl -u review-collector -f
```

### Docker

```bash
docker build -t a3-review-collector .
docker run -d --name a3-collector --restart unless-stopped \
  -p 8080:8080 \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/logs:/app/logs" \
  a3-review-collector
```

The `-v .../data` volume is what makes reviews survive container restarts.

---

## 8. A3 integration

A3 needs exactly two calls. Point it at the collector and give it the API key.

**Step 1 — poll for new reviews**

```http
GET /api/reviews/new
Authorization: Bearer <A3_API_KEY>
```

**Step 2 — acknowledge each one after responding**

```http
POST /api/reviews/{review_id}/processed
Authorization: Bearer <A3_API_KEY>
```

Once acknowledged, that review never appears in `/api/reviews/new` again.

### Drop-in Python client for A3

```python
import requests

COLLECTOR = "http://localhost:8080"
API_KEY = "..."                      # same value as A3_API_KEY
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

def fetch_and_respond():
    response = requests.get(f"{COLLECTOR}/api/reviews/new",
                            headers=HEADERS, timeout=30)
    response.raise_for_status()

    for review in response.json()["reviews"]:
        # ---- your existing A3 Review Responder logic ----
        generate_and_post_response(
            business=review["business_name"],
            reviewer=review["reviewer_name"],
            rating=review["rating"],
            text=review["review_text"],
        )
        # -------------------------------------------------

        # Only acknowledge AFTER you have succeeded. If A3 crashes first,
        # the review stays queued and comes back on the next poll.
        requests.post(
            f"{COLLECTOR}/api/reviews/{review['review_id']}/processed",
            headers=HEADERS, timeout=30,
        ).raise_for_status()
```

Poll every few minutes; the collector itself refreshes every 15.

**Acknowledge only after A3 has genuinely finished.** That ordering is what makes
the pipeline safe against crashes. If you need to undo an acknowledgement,
`POST /api/reviews/{review_id}/unprocessed` puts it back in the queue.

### Review payload

```json
{
  "business_name": "Mercedes-Benz of Fort Walton Beach",
  "business_id": 2,
  "business_key": "mb_fwb",
  "place_id": "ChIJ8SSirEM-kYgRPatxgc1nH88",
  "review_id": "mb_fwb:Ci9DQUlRQUNvZENodHljRjlvT21GSk4zRmFhSEZUVGpsUVZrNUNjalY2VHpRMExXYxAB",
  "reviewer_name": "Frank Ferrara",
  "reviewer_profile_url": "https://www.google.com/maps/contrib/...",
  "rating": 5,
  "star_rating": 5,
  "review_text": "Sales Representative Mr. Johnny Nash was outstanding...",
  "review_date": "2026-02-17T15:22:00Z",
  "review_date_is_approximate": true,
  "review_url": "https://search.google.com/local/reviews?placeid=...",
  "source": "google_maps",
  "detected_at": "2026-08-19T06:43:36Z",
  "processed": false,
  "processed_at": null
}
```

Notes for A3:

- Every timestamp is **UTC**, suffixed with `Z`.
- Any field Google did not publish is `null`. A five-star review with no written
  comment has `review_text: null` — that is normal, not an error. Nothing is
  ever invented to fill a gap.
- `review_date_is_approximate: true` means the date was derived from a relative
  label like "2 weeks ago" and is accurate to roughly a day. The `gbp_api`
  backend always returns `false` here.
- `review_id` is namespaced with the business key, so it is globally unique and
  safe to use as a primary key on the A3 side.

---

## 9. API reference

Every endpoint except `/health` requires:

```
Authorization: Bearer <A3_API_KEY>
```

`X-API-Key: <key>` also works, for convenience with tools that dislike Bearer.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness. **No auth.** |
| `GET` | `/api/reviews/new` | Unprocessed reviews, oldest first. `?limit=` `?business=` `?min_rating=` `?max_rating=` |
| `POST` | `/api/reviews/{review_id}/processed` | Mark processed. Idempotent. |
| `POST` | `/api/reviews/{review_id}/unprocessed` | Return to the queue. |
| `GET` | `/api/reviews` | All reviews, newest first. `?limit=` `?offset=` `?processed=` `?business=` |
| `GET` | `/api/reviews/{review_id}` | One review. |
| `GET` | `/api/admin/status` | Everything the dashboard shows. |
| `GET` | `/api/admin/checks` | Recent check history. |
| `POST` | `/api/admin/check-now` | Check both dealerships now. |
| `POST` | `/api/admin/scheduler/start\|stop\|restart` | Scheduler control. |

Auth failures: `401` missing credentials, `403` wrong key, `503` when
`A3_API_KEY` is unset on the server.

### `/health`

```json
{
  "status": "ok",
  "detail": null,
  "database": "ok",
  "scheduler_running": true,
  "last_check": "2026-08-19T14:00:00Z",
  "last_successful_check": "2026-08-19T14:00:00Z",
  "next_check": "2026-08-19T14:15:00Z",
  "check_interval_minutes": 15,
  "businesses_configured": 2,
  "businesses_healthy": 2,
  "total_reviews_collected": 148,
  "unprocessed_reviews": 3
}
```

`status` is honest about whether collection is actually working:

| Value | Meaning |
|---|---|
| `ok` | Running, and the latest check of **every** dealership succeeded. |
| `degraded` | Running, but at least one dealership is failing to collect. `detail` names it. |
| `error` | Database unreachable or scheduler stopped. |

It deliberately contains no API key, no Place IDs and no review content, so it
is safe to expose to an uptime monitor.

---

## 10. Dashboard

Open <http://localhost:8080/>. It asks once for the API key and stores it in your
browser's `localStorage`.

Shows per dealership: status, last check, new reviews found, unprocessed count,
total reviews, initial-sync state, and the full error text when a check fails.
Plus overall totals, backend availability, the recent check log, and a
**Check Now** button that runs both dealerships immediately and reports the real
outcome of each — including failures.

It auto-refreshes every 30 seconds.

---

## 11. Testing

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The offline suite (80 tests) covers your numbered requirements:

| Your test | Covered by |
|---|---|
| 1. Detect a new BMW review | `test_detects_new_bmw_review` |
| 2. Detect a new Mercedes-Benz review | `test_detects_new_mercedes_review` |
| 3. Run twice, no duplicates | `test_running_twice_creates_no_duplicates`, `test_duplicates_within_one_batch_are_collapsed` |
| 4. `/api/reviews/new` returns unprocessed | `test_new_endpoint_returns_unprocessed_reviews` |
| 5. A3 authenticates and retrieves | `test_new_endpoint_returns_unprocessed_reviews` |
| 6. Mark a review processed | `test_mark_processed_removes_review_from_the_queue` |
| 7. Processed review disappears | same test, plus `test_collector_never_resends_a_processed_review` |
| 8. Restart, data persists | `test_reviews_survive_an_application_restart` |
| 9. One dealership fails, other continues | `test_one_dealership_failing_does_not_stop_the_other` |
| 10. Dashboard Check Now | `test_check_now_button_triggers_both_dealerships` |
| 11. API-key authentication | `test_api_requires_authentication` and neighbours |
| 12. Automatic scheduler | `test_scheduler_actually_fires_the_collection_job` |

These use a temporary database and a stub in place of the network, so they are
deterministic and run offline. The stub is a test double for *Google*, not fake
product data — no fixture review can ever reach your real database.

### Live tests against the real profiles

```bash
RUN_LIVE_TESTS=1 pytest tests/test_live_collection.py -v -s
```

These really do hit your two Google Business Profiles.

`test_live_place_ids_point_at_fort_walton_beach` **passes** — it loads each
listing in a real browser and asserts the heading matches and the page says Fort
Walton Beach, never Fort Washington.

`test_live_collect_real_reviews` depends on which layout Google serves you (see
[section 1](#1-google-access-reality-check)). It has genuinely passed and
genuinely failed on the same machine within the same hour. A failure here prints
the real reason rather than hiding it. Raise the attempt count with
`LIVE_ATTEMPTS=10`.

---

## 12. Deployment options and free-tier limits

You asked me to verify free tiers rather than assume. Here is the honest picture.

### Recommended: your own Mac or an existing machine — genuinely $0

You already own the hardware. It supports persistent execution, in-process
scheduling, Chromium, and a durable SQLite file. Set it up with launchd
(section 7). This is the only option with **no** caveats.

### Free cloud tiers

| Host | Verdict |
|---|---|
| **Oracle Cloud Always Free** | Best free cloud fit. Always-free ARM VMs, persistent disk, root access, runs Chromium fine. Caveat: capacity for the free ARM shapes is frequently unavailable in popular regions. |
| **Google Cloud e2-micro free tier** | One always-free `e2-micro` VM. Enough for the `gbp_api` backend. Tight for Chromium — 1 GB RAM means you should add swap. Requires a billing account on file. |
| **Fly.io** | No longer has a meaningful always-free allowance; small apps cost a few dollars a month. **Not $0.** |
| **Render / Railway free tiers** | Free web services sleep on inactivity and have ephemeral disks. Sleeping breaks the scheduler; ephemeral disk destroys the SQLite file. **Not suitable.** |
| **Vercel / Netlify / Cloudflare Workers** | Serverless. No long-running process, no persistent local disk, no Chromium. **Not suitable.** |
| **GitHub Actions on a schedule** | Free minutes on public repos, and it *can* run Chromium. But there is no persistent disk between runs, so the database would have to be committed back to the repo — workable but ugly, and the minimum interval is ~5 minutes with no guarantee. Viable fallback, not recommended. |

**Recommendation: run it on hardware you already control.** That is the only way
to satisfy "$0 recurring, persistent execution, scheduled checks, browser
automation, and database persistence" simultaneously.

### Other limits worth knowing

- **SQLite** — ideal here. Tens of thousands of reviews are nothing for it, and
  WAL mode handles one writer plus concurrent readers, which is exactly this
  workload.
- **Google Business Profile API** — free; default quota 300 QPM once approved.
  Two dealerships every 15 minutes uses about 8 calls an hour.
- **Chromium** — roughly 150 MB on disk and 300–500 MB RAM while a check runs.
  Budget at least 1 GB RAM, or use `gbp_api`, which needs no browser at all.
- **Rate limiting** — checks are sequential, not parallel, and 15 minutes is a
  polite interval. Do not drop it to seconds.

---

## 13. Troubleshooting

### Every API call returns 503

`A3_API_KEY` is not set in `.env`. Generate one and restart. The service refuses
to run unauthenticated rather than silently exposing your data.

### Dashboard says "limited view" / checks keep failing with `AccessBlocked`

Google is serving your network the reduced listing. This is the situation
described in [section 1](#1-google-access-reality-check). Confirm with:

```bash
python scripts/diagnose_maps.py
```

Fixes, best first:
1. Set up `COLLECTOR_BACKEND=gbp_api` (free, reliable — section 5).
2. Leave it running. The layout is intermittent; later checks often succeed.
3. Try from a different network or a US-based machine.

Do **not** try to work around it with proxies or CAPTCHA solvers. That is
outside this project's scope by design.

### `AccessBlocked: Google served a CAPTCHA`

Same conclusion. Reduce check frequency and switch to `gbp_api`.

### `Chromium is not installed`

```bash
python -m playwright install chromium
```

### `Playwright could not start: ... inside the asyncio loop`

You are calling the sync Playwright API from async code. The collector already
isolates this on worker threads; if you see it, you have added an `async def`
route that calls a backend directly. Make the route a plain `def`.

### GBP API returns 403

Either access has not been approved yet — check the quota in Cloud Console; 0 QPM
means pending, 300 QPM means approved — or the authorised Google account does not
manage that location. Re-run `scripts/gbp_list_locations.py`.

### GBP API returns 404

`BMW_FWB_GBP_LOCATION` / `MB_FWB_GBP_LOCATION` is wrong. Run
`scripts/gbp_list_locations.py` and copy the exact `accounts/*/locations/*` value.

### Refresh token rejected

Revoke the app at <https://myaccount.google.com/permissions> and re-run
`scripts/gbp_authorize.py`.

### A3 keeps receiving the same review

It is not acknowledging them. After processing, A3 must call
`POST /api/reviews/{review_id}/processed`. Verify:

```bash
curl -H "Authorization: Bearer $A3_API_KEY" \
  "http://localhost:8080/api/reviews?processed=false&limit=5"
```

### A3 received nothing after the first run

Expected, if `INITIAL_SYNC_MARK_PROCESSED=true` (the default). The back-catalogue
is stored already marked processed so A3 is not flooded with years of history.
Only reviews found *after* that first sync are queued. To feed history to A3
instead, set it to `false` and delete `data/reviews.db` before the first run.

### Reviews collected but dates look wrong

Public Maps pages only publish relative dates. Those reviews carry
`review_date_is_approximate: true` and are accurate to about a day. Use
`gbp_api` for exact timestamps.

### Database is locked

Two instances are running against one file. Check with `ps aux | grep uvicorn`.
Run only one.

### Nothing is being checked automatically

```bash
curl http://localhost:8080/health
```

If `scheduler_running` is `false`, either `SCHEDULER_ENABLED=false` in `.env`, or
the scheduler was stopped via the API. Restart with
`POST /api/admin/scheduler/restart`.

### Where are the logs?

`logs/collector.log` (rotating, 5 MB × 5) and stdout.

```bash
tail -f logs/collector.log
```

---

## 14. How it works internally

```
review-collector/
├── app/
│   ├── main.py                     FastAPI app; starts DB + scheduler on boot
│   ├── config.py                   .env -> typed settings
│   ├── logging_setup.py            console + rotating file logs
│   ├── api/
│   │   ├── deps.py                 Bearer API-key auth (constant-time compare)
│   │   ├── reviews.py              the A3 contract
│   │   ├── admin.py                dashboard + operations
│   │   └── health.py               public liveness
│   ├── collector/                  <-- ALL Google-specific code lives here
│   │   ├── base.py                 RawReview + backend interface + error types
│   │   ├── gbp_api.py              official Business Profile API backend
│   │   ├── google_maps.py          public Maps page backend (Playwright)
│   │   ├── parser.py               pure parsing: ratings, dates, text
│   │   ├── normalizer.py           RawReview -> database row
│   │   ├── deduplication.py        review IDs and fingerprints
│   │   └── registry.py             backend selection
│   ├── database/
│   │   ├── models.py               Business, Review, CheckRun
│   │   └── database.py             engine, sessions, WAL
│   ├── scheduler/scheduler.py      APScheduler; start/stop/restart/trigger
│   └── services/
│       ├── collection_service.py   orchestration, retries, failure isolation
│       └── stats.py                dashboard aggregates
├── dashboard/                      static HTML/CSS/JS
├── scripts/                        place-ID lookup, diagnostics, GBP OAuth
├── tests/                          80 tests
└── deploy/                         systemd unit
```

### Replacing the Google layer

Everything Google-specific is behind `CollectorBackend`. To add a new source,
implement `is_available()` and `collect(business, limit)` returning
`CollectionResult`, and register it in `registry.py`. The API, database,
scheduler, dashboard and A3 integration need no changes. If Google merely
restyles Maps, only the selectors in `google_maps.py` and the mapping functions
in `parser.py` need attention.

### How duplicates are prevented

Two independent layers:

1. **A stable `review_id`.** Google's own permanent ID when available, prefixed
   with the business key so the two dealerships can never collide. When no
   stable ID exists, a SHA-256 fingerprint of reviewer, rating, text and date —
   with the date bucketed to the day, because relative dates drift between
   checks and an un-bucketed timestamp would re-fingerprint the same review on
   every run.
2. **A `UNIQUE` constraint on `reviews.review_id`.** Even if a scheduled check
   and a manual **Check Now** race each other, the database rejects the loser.
   Inserts are flushed one at a time so a single duplicate cannot roll back the
   genuinely new reviews alongside it.

### How one dealership failing cannot affect the other

Each business is collected in its own try/except and its own transaction. A
failure records a `CheckRun` row with the error type, message, timing, and
whether a retry will happen — then the loop moves to the next business. The
scheduler keeps running; the next cycle retries the failed one. This is verified
by `test_one_dealership_failing_does_not_stop_the_other`, and was observed live:
BMW failed with `AccessBlocked` while Mercedes-Benz collected 5 real reviews in
the same cycle.

### Log output

```
[2026-08-19 14:39:44] INFO  collector | Review check cycle started (scheduled) for 2 business(es)
[2026-08-19 14:39:44] INFO  collector | BMW of Fort Walton Beach: check started
[2026-08-19 14:40:14] ERROR collector | BMW of Fort Walton Beach: check FAILED after 30183ms |
                                        operation=collect | cause=AccessBlocked: ... |
                                        will retry automatically at the next scheduled check (in ~15 min)
[2026-08-19 14:40:14] INFO  collector | Mercedes-Benz of Fort Walton Beach: check started
[2026-08-19 14:43:36] INFO  collector | Mercedes-Benz of Fort Walton Beach: 5 review(s) found
[2026-08-19 14:43:36] INFO  collector | Mercedes-Benz of Fort Walton Beach: 5 new review(s), 0 existing review(s) skipped
[2026-08-19 14:43:36] INFO  collector | Mercedes-Benz of Fort Walton Beach: check complete in 27616ms via google_maps
```

Every failure states which dealership, which operation, why, when, and whether it
will retry.

---

---

## 15. Email notifications

Sends a Gmail message every time someone clicks **Check Now** or **Refresh** on
the dashboard.

### Why Gmail SMTP, and what "guaranteed Inbox" really means

No sender can *guarantee* inbox placement by itself -- that decision belongs to
Gmail's filter. Two things get you as close as possible, and the second one is
the only actual guarantee:

1. **Send from your Google account to your Google account.** The message is
   authenticated by Google, DKIM-signed with Google's key, and never leaves
   Google's infrastructure, so there is no SPF/DKIM/DMARC misalignment to punish.
   Free third-party relays (SendGrid, Mailgun, Brevo) send from shared IP pools
   with mixed reputation and land in Spam far more often.
2. **Add a Gmail filter that says "Never send it to Spam."** This overrides the
   spam classifier outright. Set it up -- it takes 30 seconds and it is the step
   that makes the requirement actually true.

### Setup

**1. Create a Google App Password** (needs 2-Step Verification):
   <https://myaccount.google.com/apppasswords> -- it gives you 16 characters.
   Your normal Google password will **not** work.

**2. Fill in `.env`:**

```env
NOTIFY_ENABLED=true
SMTP_USERNAME=you@yourdomain.com        # the full Gmail address
SMTP_PASSWORD=abcdefghijklmnop          # the 16-char App Password
NOTIFY_TO=you@yourdomain.com            # comma-separate for several people
```

**3. Send a test message:**

```bash
python scripts/test_email.py
```

It prints the configuration, sends one real message, and explains any failure in
plain language instead of a raw SMTP traceback.

**4. Create the Gmail filter (do not skip this):**

   Gmail -> search box -> *Show search options* -> Has the words:
   `"[A3 Collector]"` -> **Create filter** -> tick **Never send it to Spam**
   (and optionally *Always mark as important*).

   The subject prefix is deliberately stable so this one filter matches every
   notification forever.

### What you get

| Event | Default | Notes |
|---|---|---|
| **Check Now** clicked | on | Includes per-dealership found/new counts and any failures |
| **Refresh** clicked | on | Manual clicks only -- see the warning below |
| Scheduled 15-minute check | off | `NOTIFY_ON_SCHEDULED_CHECK=true` |
| A new review is found | off | `NOTIFY_ON_NEW_REVIEW=true` -- arguably the most useful one |

Each email shows the event, timestamp, who triggered it, a per-dealership table
(found / new / status, with failures reported honestly), running totals, and a
link back to the dashboard.

### Important: the auto-refresh does NOT send mail

The dashboard refreshes itself every 30 seconds. If that fired a notification it
would be roughly **2,880 messages a day per open tab** -- and that volume is
exactly what causes Gmail to start filtering the sender into Spam, defeating the
whole point. Only a human clicking **Refresh** sends mail.

As a second safeguard, `NOTIFY_MIN_INTERVAL_SECONDS` (default 60) suppresses
repeat notifications of the same event type inside that window. The limit is
per event type, so a Check Now is never hidden by a recent Refresh. Set it to
`0` to disable.

### Switching the sender to an a3brands.com mailbox

The notifications currently send from a personal Gmail, which exposes a personal
address in the From line and means every recipient must whitelist an unknown
sender. Sending from the company domain fixes both: internal Workspace mail to
a3brands.com recipients is trusted and is not spam-filtered.

This needs one thing from a Workspace admin -- see "What to ask IT for" below.

Once the mailbox and its App Password exist, the switch is three lines in `.env`:

```env
SMTP_USERNAME=noreply@a3brands.com      # the sending mailbox
SMTP_PASSWORD=<its 16-char App Password>
NOTIFY_FROM_NAME=A3 Review Collector
```

Leave `NOTIFY_FROM` blank so the From stays aligned with the authenticated
mailbox -- a mismatch breaks DKIM/DMARC and lands mail in Spam.

Then restart the service and confirm with:

```bash
python scripts/test_email.py
```

Recipients on a3brands.com can then drop their "Never send it to Spam" filters,
though keeping a label filter is still handy for foldering.

**Note:** an email always carries a From address -- that is required by the mail
standard and cannot be hidden. Moving to a company mailbox changes *which*
address is shown; it does not remove it.

### Behaviour under failure

Mail is sent on a worker thread, so a slow or unreachable mail server never
delays an API response or a collection cycle. If sending fails it is logged as
an error and the check result is returned regardless -- a broken mailbox must
never break review collection.

### Troubleshooting

| Symptom | Cause |
|---|---|
| `SMTPAuthenticationError` | Using the account password instead of the 16-char App Password, or 2-Step Verification is off |
| `SMTP_PASSWORD is not set` | `.env` not filled in, or the service was not restarted after editing it |
| Nothing arrives, no error | `NOTIFY_ENABLED=false`, or the rate limit suppressed it -- check `logs/collector.log` |
| Lands in Spam | Add the Gmail filter in step 4 |
| Want mail on new reviews only | `NOTIFY_ON_CHECK_NOW=false`, `NOTIFY_ON_REFRESH=false`, `NOTIFY_ON_NEW_REVIEW=true` |

## Security notes

- No secret is ever hard-coded. `A3_API_KEY` and the Google OAuth values come
  only from the environment.
- `.gitignore` excludes `.env`, `data/` and `logs/`.
- API keys are compared with `hmac.compare_digest`.
- `/health` exposes no key, no Place ID and no review content.
- The collector only ever reads **publicly visible** review information, or data
  from profiles you are authorised to manage via OAuth.
- No CAPTCHA solving, no authentication bypass, no fingerprint spoofing, no
  proxy rotation, and no evasion of rate limits.
