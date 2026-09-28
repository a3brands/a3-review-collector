# Screenshots

Captured from the running system with Playwright, not mock-ups. What is here is
what a person actually sees.

## From an approval email

| File | What it shows |
|---|---|
| `approve-page-light.png` | The Approve screen, phone width. Shows the rating, the review, and the exact reply that will be published |
| `approve-page-dark.png` | The same page following a phone set to dark |
| `approve-page-desktop.png` | The same page on a wide screen |
| `approve-result.png` | After pressing Approve: where the reply now sits |
| `approve-link-already-used.png` | Clicking a link that has already been acted on |
| `edit-link-opens-review.png` | The Edit button landing in the dashboard with the emailed draft loaded |

## The dashboard

| File | What it shows |
|---|---|
| `dashboard-desktop.png` | The queue, sorted lowest rating first |
| `dashboard-phone.png` | The same queue on a phone |
| `ready-to-post.png` | Approved replies waiting to be pasted onto the listing |
| `backlog-mode.png` | Working the backlog, one review at a time |

## Retaking them

Playwright lives in the collector's virtualenv, which is the only interpreter
here with a browser installed:

    review-collector/.venv/bin/python your-capture-script.py

Point it at http://127.0.0.1:3999 and sign in with DASHBOARD_USER and
DASHBOARD_PASSWORD from google-reviews-manager/.env.

Captured 2026-09-07.
