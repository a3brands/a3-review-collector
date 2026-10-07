"""Email notifications for dashboard activity.

Deliverability note -- why Gmail SMTP and not a third-party sender:

  Nobody can *guarantee* inbox placement, but sending from your own Google
  account to your own Google account is as close as it gets. The message is
  authenticated by Google, signed with Google's DKIM key, and never leaves
  Google's infrastructure, so there is no SPF/DKIM/DMARC misalignment for the
  spam filter to punish. Free relays (SendGrid, Mailgun, Brevo) send from shared
  IP pools with a mixed reputation and land in Spam far more often.

  The only true guarantee is a Gmail filter with "Never send it to Spam" -- see
  the README, "Email notifications". This module sets a stable From, To and
  Subject prefix precisely so that one filter can match every message.

Sending happens on a worker thread: an SMTP round trip takes a second or two and
must never delay an API response or a collection cycle. A failure to notify is
logged loudly but never breaks a check.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Dict, List, Optional

from app.config import Settings, get_settings

logger = logging.getLogger("notifier")

_last_sent: Dict[str, float] = {}
_lock = threading.Lock()


class NotificationError(Exception):
    pass


def _rate_limit_key(event: str, results: Optional[List[Dict]]) -> str:
    """The bucket this notification is throttled against.

    Scoped to a single dealership whenever the event is about one, because all
    six are checked within seconds of each other: an event-wide bucket meant
    the first dealership to report new reviews silenced the rest for a whole
    interval, and a suppressed 'new_review' is never retried -- mark_notified()
    only runs after a delivery, so those reviews are simply never emailed.

    Cycle-wide events (a Refresh, a Check Now) cover every dealership at once,
    so they stay on one bucket per event.
    """
    if results and len(results) == 1:
        name = results[0].get("business_name")
        if name:
            return f"{event}:{name}"
    return event


def _rate_limited(key: str, min_interval: int) -> bool:
    """True when this bucket fired too recently.

    Protects the Gmail account's sending reputation -- a burst of identical
    mail is exactly what gets a sender filtered into Spam.
    """
    if min_interval <= 0:
        return False
    now = time.monotonic()
    with _lock:
        previous = _last_sent.get(key)
        if previous is not None and (now - previous) < min_interval:
            return True
        _last_sent[key] = now
    return False


def reset_rate_limit() -> None:
    with _lock:
        _last_sent.clear()


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------
def _format_results(results: List[Dict]) -> tuple[str, str]:
    """Render the per-dealership outcome as (plain text, html)."""
    if not results:
        return "No dealerships were checked.", "<p>No dealerships were checked.</p>"

    lines = []
    rows = []
    for item in results:
        name = item.get("business_name", "?")
        if item.get("status") == "success":
            lines.append(
                f"  {name}: {item.get('reviews_found', 0)} found, "
                f"{item.get('new_reviews', 0)} new, "
                f"{item.get('skipped_existing', 0)} already known"
            )
            rows.append(
                f"<tr><td>{name}</td><td>{item.get('reviews_found', 0)}</td>"
                f"<td><b>{item.get('new_reviews', 0)}</b></td>"
                f"<td style='color:#137a4d'>success</td></tr>"
            )
        else:
            lines.append(
                f"  {name}: FAILED - {item.get('error_type')}: {item.get('error_message')}"
            )
            rows.append(
                f"<tr><td>{name}</td><td>-</td><td>-</td>"
                f"<td style='color:#b4232c'>failed: {item.get('error_type')}</td></tr>"
            )

    html = (
        "<table cellpadding='6' cellspacing='0' border='0' "
        "style='border-collapse:collapse;font-family:sans-serif;font-size:14px'>"
        "<tr style='background:#f1f3f5'><th align='left'>Dealership</th>"
        "<th align='left'>Found</th><th align='left'>New</th><th align='left'>Status</th></tr>"
        + "".join(rows)
        + "</table>"
    )
    return "\n".join(lines), html


def _format_reviews(reviews: List[Dict]) -> tuple[str, str]:
    """Render the reviews still waiting for A3 -- who wrote them and what they said."""
    if not reviews:
        return (
            "  Nothing waiting. Every collected review has been processed.",
            "<p style='color:#697586'>Nothing waiting. "
            "Every collected review has been processed.</p>",
        )

    lines, cards = [], []
    for item in reviews:
        rating = item.get("rating")
        stars = ("*" * rating) if rating else "no rating"
        who = item.get("reviewer_name") or "Anonymous"
        biz = item.get("business_name") or ""
        text = item.get("review_text") or "(rating only -- no comment left)"
        when = (item.get("review_date") or "")[:10]

        # The dealership's own Google Business Profile, directly under the name.
        # Whoever reads this needs to see the review where it lives -- on the
        # public listing -- and hunting for the right dealership in Google is
        # exactly the friction that stops a reply being written.
        profile = item.get("business_profile_url") or ""

        lines.append(
            f"  [{stars}] {who} - {biz}{(' - ' + when) if when else ''}"
            + (f"\n      Profile: {profile}" if profile else "")
            + f"\n      {text}"
        )

        colour = "#b4232c" if (rating or 5) <= 2 else "#e8a317"
        profile_html = (
            f"<div style='margin-top:2px'><a href='{profile}' "
            "style='color:#1a56c4;font-size:12px;text-decoration:none'>"
            "View Google Business Profile &rarr;</a></div>"
            if profile else ""
        )
        cards.append(
            "<div style='border:1px solid #e2e5ea;border-radius:8px;padding:12px;margin:8px 0'>"
            f"<div><span style='color:{colour};font-size:15px'>"
            f"{'&#9733;' * (rating or 0)}{'&#9734;' * (5 - (rating or 0))}</span> "
            f"<b>{who}</b> <span style='color:#697586;font-size:12px'>{biz}"
            f"{(' &middot; ' + when) if when else ''}</span></div>"
            f"{profile_html}"
            f"<div style='margin-top:6px'>{text}</div></div>"
        )

    return "\n".join(lines), "".join(cards)


def build_message(
    settings: Settings,
    event: str,
    *,
    actor: Optional[str] = None,
    results: Optional[List[Dict]] = None,
    totals: Optional[Dict] = None,
    reviews: Optional[List[Dict]] = None,
    to: Optional[str] = None,
) -> EmailMessage:
    prefix = settings.notify_subject_prefix.strip()
    label = {
        "check_now": "Check Now",
        "refresh": "Dashboard Refresh",
        "scheduled_check": "Scheduled Check",
        "new_review": "New Review",
        "failure": "Collection Failed",
        "recovered": "Collection Recovered",
        "stalled": "NOTHING COLLECTED",
        "heartbeat": "Daily Summary",
    }.get(event, event)

    new_total = sum(r.get("new_reviews", 0) for r in (results or []))
    failed = [r for r in (results or []) if r.get("status") == "failed"]

    waiting = len(reviews or [])
    business = (results[0].get("business_name") if results else None) or ""

    if event == "new_review":
        # Lead with the worst rating so a 1-star cannot be missed in a list.
        ratings = [r.get("rating") for r in (reviews or []) if r.get("rating")]
        worst = min(ratings) if ratings else None
        count = len(reviews or [])
        headline = f"{count} new review(s)"
        if business:
            headline += f" - {business}"
        if worst and worst <= 2:
            headline = f"ALERT {worst}-star review - {business or 'a dealership'}"
    elif event == "stalled":
        hours = (totals or {}).get("quiet_hours", "?")
        headline = f"ALERT no reviews collected for {hours} hours"
    elif event == "heartbeat":
        ok = len([r for r in (results or []) if r.get("status") != "failed"])
        total_biz = len(results or [])
        headline = f"Daily summary - {new_total} new, {ok}/{total_biz} dealerships OK"
        broken_links = (totals or {}).get("link_problems") or []
        if broken_links:
            headline = f"ALERT daily summary - review links broken for {len(broken_links)} dealership(s)"
        if failed:
            headline = f"ALERT daily summary - {len(failed)} dealership(s) not collecting"
    elif event == "failure":
        headline = f"FAILED - {business or 'a dealership'}"
    elif event == "recovered":
        headline = f"Recovered - {business or 'a dealership'}"
    elif event in ("check_now", "scheduled_check"):
        headline = f"{label} - {new_total} new review(s)"
        if failed:
            headline += f", {len(failed)} dealership(s) failed"
    else:
        headline = f"{label} - {waiting} review(s) waiting for reply"

    subject = f"{prefix} {headline}".strip()

    text_results, html_results = _format_results(results or [])
    when = time.strftime("%Y-%m-%d %H:%M:%S %Z")

    totals = totals or {}
    totals_text = (
        f"  Total collected : {totals.get('total_reviews_collected', '?')}\n"
        f"  Waiting for reply : {totals.get('unprocessed_reviews', '?')}\n"
        f"  Processed       : {totals.get('processed_reviews', '?')}\n"
    )

    text_reviews, html_reviews = _format_reviews(reviews or [])

    # Written for whoever opens it, not for whoever built it. This used to lead
    # with the system's own name and then a log dump -- Event, When, Check
    # result, Totals -- which told a reader nothing about whether they needed to
    # do anything. It now opens with what happened, shows the reviews, and stops.
    # Run counts and totals belong in the dashboard, not in every message.
    low = [r for r in (reviews or []) if r.get("rating") and r.get("rating") <= 2]
    n = len(reviews or [])

    if event == "stalled":
        t = totals or {}
        opening = (
            f"No review has arrived from any dealership in {t.get('quiet_hours', '?')} hours."
        )
        follow = (
            f"{t.get('successful_checks_in_window', '?')} check(s) reported success in that time, "
            f"across {t.get('active_businesses', '?')} dealership(s). Checks succeeding while "
            "nothing arrives usually means the listings are loading without their reviews, "
            "which is what happens when Google throttles or serves a reduced page. "
            "Open a dealership's Google listing by hand and see whether its reviews are there."
        )
    elif event == "heartbeat":
        t = totals or {}
        ok = [r for r in (results or []) if r.get("status") != "failed"]
        found = sum(r.get("new_reviews", 0) for r in (results or []))
        if failed:
            opening = (
                f"{len(failed)} of {len(results or [])} dealerships collected nothing "
                "in the last 24 hours."
            )
            follow = (
                "Every check for them failed. Nothing new can arrive from those "
                "listings until it clears."
            )
        else:
            opening = (
                f"All {len(ok)} dealerships collecting normally. "
                f"{found} new review{'s' if found != 1 else ''} in the last 24 hours."
            )
            # Says the quiet part out loud: this mail exists so that its
            # absence means something.
            follow = (
                f"{t.get('unprocessed_reviews', '?')} waiting for a reply. "
                "If this message does not arrive tomorrow, the collector is not running."
            )
        broken_links = t.get("link_problems") or []
        if broken_links:
            names = ", ".join(p["business_name"] for p in broken_links)
            follow += (
                f" Review links for {names} open Google without the dealership's name. "
                "Run scripts/repair_review_links.py --apply in review-collector, then "
                "scripts/verify_review_links.py to confirm."
            )
    elif event == "failure":
        opening = f"Reviews could not be collected for {business or 'a dealership'}."
        follow = "Nothing new can arrive until this clears. It will keep retrying."
    elif event == "recovered":
        opening = f"Collection is working again for {business or 'a dealership'}."
        follow = "Anything missed while it was down has been picked up."
    elif low:
        worst_seen = min(r["rating"] for r in low)
        opening = (
            f"A {worst_seen}-star review came in"
            + (f" for {business}." if business else ".")
        )
        follow = "Worth replying to this one first."
    elif n:
        opening = (
            f"{n} new review{'s' if n != 1 else ''} came in"
            + (f" for {business}." if business else ".")
        )
        follow = ""
    else:
        opening = "Nothing new since the last check."
        follow = ""

    # A dealership that could not be collected must never be hidden behind
    # "Nothing new since the last check." -- that sentence is true of the
    # reviews and false about the system, and the reader has no way to tell the
    # difference. The standing failure/recovery alerts fire on the transition
    # only, so a run that fails while somebody is watching would otherwise say
    # nothing at all.
    if failed and event not in ("failure", "recovered"):
        trouble = ", ".join(
            f"{r.get('business_name') or 'a dealership'} ({r.get('error_type') or 'FAILED'})"
            for r in failed
        )
        follow = (follow + " " if follow else "") + (
            f"Collection FAILED for {trouble}. Nothing new can arrive from "
            "there until it clears."
        )

    body = (
        f"{opening}\n"
        + (f"{follow}\n" if follow else "")
        + "\n"
        f"{when}\n\n"
        f"{text_reviews}\n"
        + (f"Dashboard: {settings.dashboard_url()}\n"
           if not any(x in settings.dashboard_url().lower() for x in ("127.0.0.1", "localhost"))
           else "")
    )

    html = (
        "<div style='font-family:sans-serif;font-size:15px;line-height:1.55;color:#1c2530'>"
        f"<p style='margin:0 0 4px;font-size:17px'><b>{opening}</b></p>"
        + (f"<p style='margin:0 0 18px;color:#8a5a00'>{follow}</p>" if follow
           else "<p style='margin:0 0 18px'></p>")
        + (html_reviews if n else "")
        + _dashboard_button(settings.dashboard_url())
        + f"<p style='margin:16px 0 0;color:#8593a0;font-size:12px'>{when}</p>"
        "</div>"
    )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((settings.notify_from_name, settings.resolved_notify_from()))
    message["To"] = to or ", ".join(settings.notify_recipients())
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="a3reviewcollector.local")
    # Helps Gmail thread these together and marks them as automated, which is
    # honest and keeps them out of the "suspicious bulk mail" bucket.
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    message.set_content(body)
    message.add_alternative(html, subtype="html")
    return message


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def _dashboard_button(url: str) -> str:
    """A link to the dashboard, or nothing.

    A localhost address is useless to everyone except whoever is sitting at
    this machine, and a plain-HTTP link to 127.0.0.1 in a message sent across
    the internet is exactly the shape of thing spam filters mark down. Better
    to send no link than a broken one.
    """
    if not url:
        return ""
    lowered = url.lower()
    if "127.0.0.1" in lowered or "localhost" in lowered:
        return ""
    return (
        f"<p style='margin:22px 0 0'><a href='{url}' "
        "style='background:#1f5e45;color:#fff;padding:10px 18px;text-decoration:none;"
        "border-radius:6px;display:inline-block'>Open the dashboard</a></p>"
    )


def _oauth_access_token(settings: Settings) -> str:
    """Trade the stored refresh token for a short-lived Gmail access token."""
    import json
    import urllib.parse
    import urllib.request

    body = urllib.parse.urlencode({
        "client_id": settings.gmail_oauth_client_id,
        "client_secret": settings.gmail_oauth_client_secret,
        "refresh_token": settings.gmail_oauth_refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    with urllib.request.urlopen(
        urllib.request.Request("https://oauth2.googleapis.com/token", data=body),
        timeout=settings.notify_timeout_seconds,
    ) as response:
        return json.load(response)["access_token"]


def _oauth_login(server: smtplib.SMTP, settings: Settings) -> None:
    """Sign in to Gmail with OAuth (XOAUTH2) instead of an App Password.

    Used when the sending account's domain blocks the 2-Step Verification
    that App Passwords require.
    """
    import base64

    token = _oauth_access_token(settings)
    raw = f"user={settings.smtp_username}\x01auth=Bearer {token}\x01\x01"
    code, reply = server.docmd("AUTH", "XOAUTH2 " + base64.b64encode(raw.encode()).decode())
    if code != 235:
        raise smtplib.SMTPAuthenticationError(code, reply)


def _send_sync(settings: Settings, message: EmailMessage) -> None:
    host = settings.smtp_host
    port = settings.smtp_port
    context = ssl.create_default_context()

    last_error: Optional[Exception] = None
    for attempt in range(1, settings.notify_max_retries + 2):
        try:
            use_implicit_tls = port == 465 and settings.smtp_use_tls
            if use_implicit_tls:
                server = smtplib.SMTP_SSL(host, port, timeout=settings.notify_timeout_seconds, context=context)
            else:
                server = smtplib.SMTP(host, port, timeout=settings.notify_timeout_seconds)
            with server:
                server.ehlo()
                if settings.smtp_use_tls and not use_implicit_tls:
                    server.starttls(context=context)
                    server.ehlo()
                if settings.smtp_username and settings.gmail_oauth_refresh_token:
                    _oauth_login(server, settings)
                elif settings.smtp_username:
                    server.login(settings.smtp_username, settings.smtp_password)
                server.send_message(message)
            logger.info(
                "Notification sent: %r -> %s", message["Subject"], message["To"]
            )
            return
        except smtplib.SMTPAuthenticationError as exc:
            # Not retryable -- the credentials are simply wrong.
            raise NotificationError(
                "Gmail rejected the SMTP login. With 2-Step Verification on you must use a "
                "16-character App Password, not your normal account password. "
                "See README 'Email notifications'. Google said: "
                f"{exc.smtp_error!r}"
            ) from exc
        except Exception as exc:
            last_error = exc
            if attempt <= settings.notify_max_retries:
                logger.warning(
                    "Notification attempt %d failed (%s); retrying", attempt, exc
                )
                time.sleep(2 * attempt)

    raise NotificationError(f"Could not send notification after retries: {last_error}")


def send_event(
    event: str,
    *,
    actor: Optional[str] = None,
    results: Optional[List[Dict]] = None,
    totals: Optional[Dict] = None,
    reviews: Optional[List[Dict]] = None,
    settings: Optional[Settings] = None,
    blocking: bool = False,
    on_delivered=None,
) -> Dict:
    """Queue a notification. Never raises -- notifying must not break a check."""
    settings = settings or get_settings()

    if not settings.notify_enabled:
        return {"sent": False, "reason": "NOTIFY_ENABLED is false"}

    enabled_for = {
        "check_now": settings.notify_on_check_now,
        "refresh": settings.notify_on_refresh,
        "scheduled_check": settings.notify_on_scheduled_check,
        "new_review": settings.notify_on_new_review,
        "failure": settings.notify_on_failure,
        "recovered": settings.notify_on_recovery,
        "heartbeat": settings.notify_on_heartbeat,
    }
    if not enabled_for.get(event, False):
        return {"sent": False, "reason": f"notifications for '{event}' are disabled"}

    problem = settings.notify_config_problem()
    if problem:
        logger.error("Cannot send notification: %s", problem)
        return {"sent": False, "reason": problem}

    bucket = _rate_limit_key(event, results)
    if _rate_limited(bucket, settings.notify_min_interval_seconds):
        logger.info(
            "Notification for '%s' suppressed by the %ds rate limit",
            bucket, settings.notify_min_interval_seconds,
        )
        return {"sent": False, "reason": "rate limited"}

    recipients = settings.notify_recipients()

    # One message per recipient, each addressed only to that person. A shared
    # To header would expose every colleague's address to everyone else.
    messages = [
        build_message(
            settings, event, actor=actor, results=results,
            totals=totals, reviews=reviews, to=recipient,
        )
        for recipient in recipients
    ]
    subject = messages[0]["Subject"] if messages else ""

    def deliver() -> Dict:
        sent, failed = [], []
        for recipient, message in zip(recipients, messages):
            try:
                _send_sync(settings, message)
                sent.append(recipient)
            except Exception as exc:
                # One bad address must not stop the rest.
                failed.append(recipient)
                logger.error("Notification to %s failed: %s", recipient, exc)
        if sent and on_delivered is not None:
            # Only after at least one recipient actually received it, so a
            # failed send never silently marks a review as already emailed.
            try:
                on_delivered()
            except Exception:  # pragma: no cover - bookkeeping must not break sending
                logger.exception("Post-delivery bookkeeping failed")

        outcome = {"sent": bool(sent), "recipients": sent, "failed": failed, "subject": subject}
        if not sent:
            outcome["reason"] = (
                f"delivery failed for all {len(recipients)} recipient(s); see the log"
                if recipients else "no recipients configured"
            )
        return outcome

    if blocking:
        return deliver()

    threading.Thread(target=deliver, name=f"notify-{event}", daemon=True).start()
    return {"sent": True, "queued": True, "recipients": recipients, "subject": subject}
