"""Email notifications.

Uses a real in-process SMTP server on localhost, so the whole path -- message
construction, SMTP dialogue, threading, rate limiting -- is genuinely exercised
without touching Gmail or needing credentials.
"""
from __future__ import annotations

import asyncio
import email
import threading
import time

import pytest

from app.config import Settings
from app.services import notifier


class CaptureSMTP(threading.Thread):
    """Minimal SMTP server that records the messages it receives."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.messages: list = []
        self.port: int | None = None
        self._loop = None
        self._ready = threading.Event()

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        server = self._loop.run_until_complete(
            self._loop.create_server(lambda: _Proto(self.messages), "127.0.0.1", 0)
        )
        self.port = server.sockets[0].getsockname()[1]
        self._ready.set()
        self._loop.run_forever()

    def start_and_wait(self) -> "CaptureSMTP":
        self.start()
        assert self._ready.wait(10), "SMTP test server did not start"
        return self

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)


class _Proto(asyncio.Protocol):
    def __init__(self, sink: list) -> None:
        self.sink = sink
        self.buffer = b""
        self.in_data = False
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        transport.write(b"220 test ESMTP\r\n")

    def data_received(self, data: bytes) -> None:
        if self.in_data:
            self.buffer += data
            if b"\r\n.\r\n" in self.buffer:
                raw = self.buffer.split(b"\r\n.\r\n")[0]
                self.sink.append(email.message_from_bytes(raw))
                self.in_data = False
                self.buffer = b""
                self.transport.write(b"250 OK\r\n")
            return

        for line in data.split(b"\r\n"):
            if not line:
                continue
            upper = line.upper()
            if upper.startswith(b"EHLO") or upper.startswith(b"HELO"):
                self.transport.write(b"250-test\r\n250 AUTH LOGIN PLAIN\r\n")
            elif upper.startswith(b"MAIL") or upper.startswith(b"RCPT"):
                self.transport.write(b"250 OK\r\n")
            elif upper.startswith(b"DATA"):
                self.in_data = True
                self.transport.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
            elif upper.startswith(b"QUIT"):
                self.transport.write(b"221 Bye\r\n")
                self.transport.close()
            elif upper.startswith(b"AUTH"):
                self.transport.write(b"235 Authenticated\r\n")


@pytest.fixture
def smtp_server():
    server = CaptureSMTP().start_and_wait()
    yield server
    server.stop()


@pytest.fixture
def notify_settings(smtp_server):
    notifier.reset_rate_limit()
    return Settings(
        NOTIFY_ENABLED="true",
        SMTP_HOST="127.0.0.1",
        SMTP_PORT=str(smtp_server.port),
        SMTP_USERNAME="collector@example.com",
        SMTP_PASSWORD="app-password-here",
        NOTIFY_TO="inbox@example.com",
        NOTIFY_MIN_INTERVAL_SECONDS="0",
        SMTP_USE_TLS="false",   # the in-process test server speaks plaintext
    )


RESULTS = [
    {"business_name": "BMW of Fort Walton Beach", "status": "success",
     "reviews_found": 20, "new_reviews": 2, "skipped_existing": 18},
    {"business_name": "Mercedes-Benz of Fort Walton Beach", "status": "failed",
     "error_type": "AccessBlocked", "error_message": "reduced listing"},
]
TOTALS = {"total_reviews_collected": 403, "unprocessed_reviews": 3, "processed_reviews": 400}


def test_check_now_sends_a_notification(smtp_server, notify_settings):
    result = notifier.send_event(
        "check_now", actor="127.0.0.1", results=RESULTS, totals=TOTALS,
        settings=notify_settings, blocking=True,
    )

    assert result["sent"] is True
    assert len(smtp_server.messages) == 1

    message = smtp_server.messages[0]
    assert message["To"] == "inbox@example.com"
    assert message["From"] == "A3 Review Collector <collector@example.com>"
    assert message["Subject"].startswith("[A3 Collector]")
    assert "Check Now" in message["Subject"]
    assert "2 new review(s)" in message["Subject"]
    assert "1 dealership(s) failed" in message["Subject"]


def test_notification_body_reports_a_dealership_that_failed(smtp_server, notify_settings):
    """A failed dealership is never hidden behind "nothing new".

    The per-run table and the totals block were deliberately removed from these
    emails -- they belong on the dashboard. What must survive that is the one
    fact a reader cannot get anywhere else: a dealership stopped collecting, so
    silence from it does not mean silence from its customers.
    """
    notifier.send_event("check_now", results=RESULTS, totals=TOTALS,
                        settings=notify_settings, blocking=True)

    body = smtp_server.messages[0].get_payload(0).get_payload()
    html = smtp_server.messages[0].get_payload(1).get_payload()
    assert "Mercedes-Benz of Fort Walton Beach" in body
    assert "FAILED" in body and "AccessBlocked" in body   # failures reported honestly
    assert "FAILED" in html and "AccessBlocked" in html


def test_refresh_sends_its_own_notification(smtp_server, notify_settings):
    result = notifier.send_event("refresh", actor="127.0.0.1", totals=TOTALS,
                                 settings=notify_settings, blocking=True)
    assert result["sent"] is True
    assert "Dashboard Refresh" in smtp_server.messages[0]["Subject"]


def test_deliverability_headers_are_present(smtp_server, notify_settings):
    """Stable From/Subject + honest automation headers keep Gmail filters happy."""
    notifier.send_event("check_now", results=RESULTS, totals=TOTALS,
                        settings=notify_settings, blocking=True)
    message = smtp_server.messages[0]

    assert message["Auto-Submitted"] == "auto-generated"
    assert message["X-Auto-Response-Suppress"] == "All"
    assert message["Message-ID"]
    assert message["Date"]
    assert message.is_multipart()  # plain text + HTML


def test_from_address_defaults_to_the_authenticated_mailbox(notify_settings):
    """A mismatched From breaks DKIM alignment and lands mail in Spam."""
    assert notify_settings.resolved_notify_from() == "collector@example.com"


def test_rate_limit_suppresses_a_burst(smtp_server, notify_settings):
    limited = notify_settings.model_copy(update={"notify_min_interval_seconds": 300})
    notifier.reset_rate_limit()

    first = notifier.send_event("refresh", totals=TOTALS, settings=limited, blocking=True)
    second = notifier.send_event("refresh", totals=TOTALS, settings=limited, blocking=True)

    assert first["sent"] is True
    assert second["sent"] is False
    assert second["reason"] == "rate limited"
    assert len(smtp_server.messages) == 1


def test_rate_limit_is_per_dealership(smtp_server, notify_settings):
    """A busy cycle must not let one dealership silence the others.

    All six are checked within seconds, so an event-wide bucket dropped every
    dealership after the first -- permanently, since a suppressed 'new_review'
    is never retried.
    """
    limited = notify_settings.model_copy(update={"notify_min_interval_seconds": 300})
    notifier.reset_rate_limit()

    def new_review_for(name):
        return notifier.send_event(
            "new_review",
            results=[{"business_name": name, "status": "success",
                      "reviews_found": 20, "new_reviews": 1, "skipped_existing": 19}],
            totals=TOTALS, settings=limited, blocking=True,
        )

    first = new_review_for("BMW of Fort Walton Beach")
    second = new_review_for("Findlay Subaru of Las Vegas")
    third = new_review_for("Parks Lincoln")

    assert first["sent"] is True
    assert second["sent"] is True
    assert third["sent"] is True
    assert len(smtp_server.messages) == 3

    # The same dealership twice inside the window is still a burst.
    repeat = new_review_for("BMW of Fort Walton Beach")
    assert repeat["sent"] is False
    assert repeat["reason"] == "rate limited"
    assert len(smtp_server.messages) == 3


def test_rate_limit_is_per_event_type(smtp_server, notify_settings):
    limited = notify_settings.model_copy(update={"notify_min_interval_seconds": 300})
    notifier.reset_rate_limit()

    notifier.send_event("refresh", totals=TOTALS, settings=limited, blocking=True)
    other = notifier.send_event("check_now", results=RESULTS, totals=TOTALS,
                                settings=limited, blocking=True)

    assert other["sent"] is True   # a Check Now is never hidden by a Refresh
    assert len(smtp_server.messages) == 2


def test_disabled_notifications_send_nothing(smtp_server, notify_settings):
    off = notify_settings.model_copy(update={"notify_enabled": False})
    result = notifier.send_event("check_now", results=RESULTS, settings=off, blocking=True)
    assert result["sent"] is False
    assert not smtp_server.messages


def test_refresh_can_be_disabled_independently(smtp_server, notify_settings):
    off = notify_settings.model_copy(update={"notify_on_refresh": False})
    assert notifier.send_event("refresh", settings=off, blocking=True)["sent"] is False
    assert notifier.send_event("check_now", results=RESULTS, settings=off,
                               blocking=True)["sent"] is True


def test_missing_credentials_are_explained_not_crashed(smtp_server, notify_settings):
    broken = notify_settings.model_copy(update={"smtp_password": ""})
    result = notifier.send_event("check_now", results=RESULTS, settings=broken, blocking=True)
    assert result["sent"] is False
    assert "App Password" in result["reason"]


def test_send_failure_never_raises(notify_settings):
    """A dead mail server must not break a collection cycle."""
    broken = notify_settings.model_copy(
        update={"smtp_port": 1, "notify_max_retries": 0, "notify_timeout_seconds": 2}
    )
    result = notifier.send_event("check_now", results=RESULTS, settings=broken, blocking=True)
    assert result["sent"] is False
    assert result["reason"]


def test_non_blocking_send_does_not_delay_the_caller(smtp_server, notify_settings):
    notifier.reset_rate_limit()
    started = time.monotonic()
    result = notifier.send_event("check_now", results=RESULTS, totals=TOTALS,
                                 settings=notify_settings, blocking=False)
    elapsed = time.monotonic() - started

    assert result["queued"] is True
    assert elapsed < 0.5          # returns immediately; SMTP happens on a worker
    for _ in range(50):
        if smtp_server.messages:
            break
        time.sleep(0.1)
    assert len(smtp_server.messages) == 1


PENDING = [
    {"reviewer_name": "bryan pritchett", "rating": 5,
     "business_name": "BMW of Fort Walton Beach", "review_date": "2026-08-19T23:54:04Z",
     "review_text": "Very friendly staff. The vehicle was ready quickly."},
    {"reviewer_name": "Cecil Williams", "rating": 5,
     "business_name": "BMW of Fort Walton Beach", "review_date": "2026-08-19T23:11:04Z",
     "review_text": None},
    {"reviewer_name": "Angry Customer", "rating": 1,
     "business_name": "Mercedes-Benz of Fort Walton Beach", "review_date": "2026-08-19T19:11:27Z",
     "review_text": "Terrible service, still waiting."},
]


def test_each_review_links_to_its_dealerships_google_profile(smtp_server, notify_settings):
    """The listing the review lives on, one click from the email."""
    with_profiles = [
        dict(item, business_profile_url="https://www.google.com/maps/place/?q=place_id:ABC")
        for item in PENDING
    ]
    notifier.send_event("refresh", totals=TOTALS, reviews=with_profiles,
                        settings=notify_settings, blocking=True)

    body = smtp_server.messages[0].get_payload(0).get_payload()
    html = smtp_server.messages[0].get_payload(1).get_payload()
    assert "https://www.google.com/maps/place/?q=place_id:ABC" in body
    assert "View Google Business Profile" in html


def test_a_review_with_no_profile_configured_renders_no_link(smtp_server, notify_settings):
    notifier.send_event("refresh", totals=TOTALS, reviews=PENDING,
                        settings=notify_settings, blocking=True)

    html = smtp_server.messages[0].get_payload(1).get_payload()
    assert "View Google Business Profile" not in html


def test_email_names_who_left_each_waiting_review(smtp_server, notify_settings):
    """The whole point: the email must say WHO commented and WHAT they said."""
    notifier.send_event("refresh", totals=TOTALS, reviews=PENDING,
                        settings=notify_settings, blocking=True)

    body = smtp_server.messages[0].get_payload(0).get_payload()
    assert "bryan pritchett" in body
    assert "Very friendly staff" in body
    assert "Angry Customer" in body
    assert "Terrible service" in body
    assert "BMW of Fort Walton Beach" in body


def test_star_only_review_is_labelled_not_blank(smtp_server, notify_settings):
    notifier.send_event("refresh", totals=TOTALS, reviews=PENDING,
                        settings=notify_settings, blocking=True)
    body = smtp_server.messages[0].get_payload(0).get_payload()
    assert "Cecil Williams" in body
    assert "rating only" in body.lower()


def test_refresh_subject_reports_how_many_are_waiting(smtp_server, notify_settings):
    notifier.send_event("refresh", totals=TOTALS, reviews=PENDING,
                        settings=notify_settings, blocking=True)
    assert "3 review(s) waiting for reply" in smtp_server.messages[0]["Subject"]


def test_empty_queue_says_so_plainly(smtp_server, notify_settings):
    notifier.send_event("refresh", totals=TOTALS, reviews=[],
                        settings=notify_settings, blocking=True)
    body = smtp_server.messages[0].get_payload(0).get_payload()
    assert "Nothing waiting" in body


def test_multiple_recipients_all_receive_it(smtp_server, notify_settings):
    """NOTIFY_TO accepts a comma-separated list."""
    multi = notify_settings.model_copy(
        update={"notify_to": "one@a3brands.com, two@a3brands.com,three@example.com"}
    )
    assert multi.notify_recipients() == [
        "one@a3brands.com", "two@a3brands.com", "three@example.com"
    ]

    notifier.reset_rate_limit()
    result = notifier.send_event("check_now", results=RESULTS, totals=TOTALS,
                                 reviews=PENDING, settings=multi, blocking=True)

    assert result["sent"] is True
    # One message each, so nobody sees the other recipients.
    assert len(smtp_server.messages) == 3
    headers = sorted(m["To"] for m in smtp_server.messages)
    assert headers == ["one@a3brands.com", "three@example.com", "two@a3brands.com"]
    for message in smtp_server.messages:
        assert "," not in message["To"]


def test_recipient_list_tolerates_messy_spacing(notify_settings):
    messy = notify_settings.model_copy(
        update={"notify_to": "  a@x.com ,, b@y.com ,  "}
    )
    assert messy.notify_recipients() == ["a@x.com", "b@y.com"]


def test_each_recipient_sees_only_their_own_address(smtp_server, notify_settings):
    """Colleagues' addresses must not be exposed to each other."""
    multi = notify_settings.model_copy(
        update={"notify_to": "a@a3brands.com,b@a3brands.com"}
    )
    notifier.reset_rate_limit()
    notifier.send_event("check_now", results=RESULTS, totals=TOTALS,
                        reviews=PENDING, settings=multi, blocking=True)

    assert len(smtp_server.messages) == 2
    for message in smtp_server.messages:
        others = {"a@a3brands.com", "b@a3brands.com"} - {message["To"]}
        for other in others:
            assert other not in message.as_string()


def test_one_bad_address_does_not_stop_the_others(smtp_server, notify_settings):
    multi = notify_settings.model_copy(update={"notify_to": "good@a3brands.com,also@a3brands.com"})
    notifier.reset_rate_limit()
    result = notifier.send_event("check_now", results=RESULTS, totals=TOTALS,
                                 settings=multi, blocking=True)
    assert result["sent"] is True
    assert len(result["recipients"]) == 2
    assert result["failed"] == []


def test_email_never_talks_about_a3_internals(smtp_server, notify_settings):
    """Grace's rename, as it survives the rewrite of the email body.

    The ask was that nothing in a client-facing email is phrased in terms of
    A3's internal queue -- it used to head the review list "Waiting for A3".
    The section headings themselves are gone: the email now opens with what
    happened and shows the reviews. What it must still never do is name A3's
    processing state at the reader.
    """
    notifier.send_event("refresh", totals=TOTALS, reviews=PENDING,
                        settings=notify_settings, blocking=True)
    body = smtp_server.messages[0].get_payload(0).get_payload()
    html = smtp_server.messages[0].get_payload(1).get_payload()

    assert "Waiting for A3" not in body
    assert "Waiting for A3" not in html
    # And the reviews themselves are still there to be read.
    assert "bryan pritchett" in body
    assert "Terrible service, still waiting." in body


def test_triggering_ip_and_user_agent_are_not_in_the_email(smtp_server, notify_settings):
    """Grace asked for the 'triggered by 127.0.0.1 (Mozilla/5.0 ...)' line removed."""
    notifier.send_event("refresh", actor="127.0.0.1 (Mozilla/5.0 (Macintosh; Intel Mac OS X))",
                        totals=TOTALS, reviews=PENDING,
                        settings=notify_settings, blocking=True)
    raw = smtp_server.messages[0].as_string()
    assert "triggered by" not in raw
    assert "Mozilla" not in raw          # no user-agent string
    # The dashboard link legitimately contains 127.0.0.1, so check the actor
    # never appears as an attribution rather than banning the substring.
    assert "127.0.0.1 (Mozilla" not in raw
