# A3 Review Collector

Collects Google reviews for A3 Brands' dealerships and hands them to the
Review Responder, which drafts replies and emails them out for approval.

Six dealerships are live: BMW of Fort Walton Beach, Mercedes-Benz of Fort
Walton Beach, I-90 Nissan, Parks Lincoln, Findlay Subaru of Las Vegas and
Merit Auto Group.

## The two halves

| | What it does | Where it lives |
|---|---|---|
| **Collector** (this repo, `review-collector/`) | Reads each dealership's Google listing every 15 minutes, stores new reviews, emails alerts | Python, FastAPI, SQLite |
| **Responder** | Dashboard, AI-drafted replies, approval by email | [a3brandsanalytics/google-reviews-manager](https://github.com/a3brandsanalytics/google-reviews-manager) — Node, Postgres |

They run on the same machine and talk over `127.0.0.1`.

## How a review becomes a published reply

1. The collector finds a new review on Google.
2. It pushes the review to the responder over a signed endpoint.
3. The responder drafts a reply and emails it to the approver.
4. The approver presses Approve in the email.
5. The reply is posted to Google.

Step 5 is manual today: the Google Business Profile API has not been
provisioned, so an approved reply is handed to a person to paste in.

## Running it

```bash
make install      # create the Python venv, install both apps, fetch Chromium
make collector    # collector on :8080, with the 15-minute schedule
make dev          # responder dashboard on :3999
make test         # both test suites
make doctor       # check Chromium, Postgres, SMTP and the tailnet
make status       # is it running, and when did it last collect
```

## What is not in this repository

`.env` files, the review databases, logs and customer data are all excluded.
Copy `review-collector/.env.example` to `.env` and fill it in. The secrets it
needs are the Gmail app password, the collector API key and the dashboard
login.

## Documentation

- `docs/BMW-FWB-Responder-User-Guide.pptx` / `.pdf` — the step-by-step guide
  for dealership staff, with screenshots
- `docs/roadmap.html` — project roadmap
- `review-collector/docs/` — collector notes
- `deploy/` — server provisioning and the Tailscale layout, including why the
  dashboard is public and the collector is not

## A note on the machine

Everything currently runs on one laptop, under `launchd`. Collection pauses
while it sleeps and resumes on wake; nothing is lost, only delayed. Moving to
a always-on server is the outstanding piece of work.
