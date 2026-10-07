"""Configuration loaded from environment / .env.

Nothing secret is ever hard-coded here -- every credential comes from the
environment.  See .env.example for the full documented list.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class BusinessConfig:
    """One monitored Google Business Profile.

    ``key`` is the stable internal slug (never changes, used in logs + URLs).
    ``place_id`` / ``google_url`` pin the *exact* listing so a similarly named
    business can never be monitored by accident.
    """

    def __init__(
        self,
        key: str,
        name: str,
        google_url: Optional[str],
        place_id: Optional[str],
        gbp_location_name: Optional[str] = None,
    ) -> None:
        self.key = key
        self.name = name
        self.google_url = (google_url or "").strip() or None
        self.place_id = (place_id or "").strip() or None
        self.gbp_location_name = (gbp_location_name or "").strip() or None

    def is_configured(self) -> bool:
        return bool(self.place_id or self.google_url or self.gbp_location_name)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<BusinessConfig {self.key} place_id={self.place_id!r}>"


def dealer_slug_for(business_key: str, overrides: dict) -> Optional[str]:
    """The Responder's slug for one of our business keys.

    This used to be a two-entry dictionary, so every dealership added after the
    original pair mapped to None. The sync then logged a warning and skipped it:
    reviews were collected, stored, and never reached the dashboard, with the
    only clue an empty queue. Nothing about that looked like a failure.

    Keys here use underscores (bmw_fwb) and the Responder uses hyphens
    (bmw-fwb), so the general answer is a straight substitution. The explicit
    overrides stay first, because an existing deployment may map a key onto a
    slug that does not match it.
    """
    if not business_key:
        return None
    explicit = overrides.get(business_key)
    if explicit:
        return explicit
    return business_key.replace("_", "-") or None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", str(BASE_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------- Server ----------------
    host: str = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=8080, alias="PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_file: str = Field(default=str(BASE_DIR / "logs" / "collector.log"), alias="LOG_FILE")

    # ---------------- Database ----------------
    database_url: str = Field(
        default=f"sqlite:///{BASE_DIR / 'data' / 'reviews.db'}", alias="DATABASE_URL"
    )

    # ---------------- Scheduling ----------------
    check_interval_minutes: int = Field(default=15, alias="CHECK_INTERVAL_MINUTES")
    scheduler_enabled: bool = Field(default=True, alias="SCHEDULER_ENABLED")
    # 3 minutes: a Mac waking from sleep has no network for the first minute or
    # two, and a check (and its new-review email) run then simply fails.
    startup_check_delay_seconds: int = Field(default=180, alias="STARTUP_CHECK_DELAY_SECONDS")

    # ---------------- Adaptive pacing ----------------
    # Checking every dealership every 15 minutes spends 96% of its effort
    # learning nothing (measured over 770 checks). Each dealership is instead
    # paced by how often it actually receives reviews.
    adaptive_interval_enabled: bool = Field(default=True, alias="ADAPTIVE_INTERVAL_ENABLED")
    adaptive_window_days: int = Field(default=30, alias="ADAPTIVE_WINDOW_DAYS")
    # Reviews per day at or above which a dealership stays on the fast clock.
    adaptive_fast_per_day: float = Field(default=1.0, alias="ADAPTIVE_FAST_PER_DAY")
    adaptive_medium_per_day: float = Field(default=0.2, alias="ADAPTIVE_MEDIUM_PER_DAY")
    # Under about one review a month. At any real customer count most
    # dealerships land here, and they are the reason 500 is affordable.
    adaptive_dormant_per_day: float = Field(default=0.03, alias="ADAPTIVE_DORMANT_PER_DAY")
    adaptive_fast_minutes: int = Field(default=15, alias="ADAPTIVE_FAST_MINUTES")
    adaptive_medium_minutes: int = Field(default=60, alias="ADAPTIVE_MEDIUM_MINUTES")
    adaptive_slow_minutes: int = Field(default=240, alias="ADAPTIVE_SLOW_MINUTES")
    adaptive_dormant_minutes: int = Field(default=720, alias="ADAPTIVE_DORMANT_MINUTES")
    # Spreads each dealership's checks across its interval instead of letting
    # everything added on the same day fall due on the same tick. Without it the
    # burst, not the daily average, is what caps how many can be served.
    adaptive_stagger_enabled: bool = Field(default=True, alias="ADAPTIVE_STAGGER_ENABLED")

    # How often to look for a dealership that has never been collected for. The
    # ordinary cycle is paced in minutes, which is right for established
    # dealerships and much too slow for somebody watching a new one they just
    # added. Costs one request to the Responder per interval and nothing else.
    onboard_poll_seconds: int = Field(default=60, alias="ONBOARD_POLL_SECONDS")

    # ---------------- Parallel checks ----------------
    # Dealerships were checked strictly one after another, so the ceiling was one
    # browser's throughput no matter how much machine was underneath. Each worker
    # drives its own Chromium, which costs roughly 350MB while it runs.
    #
    # Deliberately modest. This is the only capacity change that makes Google see
    # MORE traffic at once rather than less, so it is the last lever to reach for
    # and the first one to wind back if listings start coming back empty.
    collector_workers: int = Field(default=3, alias="COLLECTOR_WORKERS")
    # Workers all start the instant a cycle begins, which would fire several page
    # loads at the same moment. A short random wait spreads that.
    collector_worker_jitter_seconds: float = Field(
        default=4.0, alias="COLLECTOR_WORKER_JITTER_SECONDS"
    )

    # ---------------- Fast path ----------------
    # The listing publishes its total review count before any scrolling. When it
    # matches what we already hold there is nothing new, so the check stops
    # there instead of scrolling the review pane.
    fast_path_enabled: bool = Field(default=True, alias="FAST_PATH_ENABLED")
    # Off, and measured that way. Skipping the warm-up visit on a fast check was
    # tried on 2026-09-03: Google served the signed-out reduced listing, which
    # carries no readable review count, so the check could not take the fast path
    # and fell through to the review pane, where it failed outright with
    # AccessBlocked. The warm-up is load-bearing, not ceremony. Leave this false
    # unless somebody re-measures and proves otherwise.
    fast_path_skip_warmup: bool = Field(default=False, alias="FAST_PATH_SKIP_WARMUP")
    # Rather than a flat wait after load, poll for the headline figures and
    # continue the moment they appear.
    listing_stats_wait_ms: int = Field(default=6000, alias="LISTING_STATS_WAIT_MS")
    # A fast check cannot see an edited or deleted review, so a full read still
    # happens this often regardless of the headline count.
    full_scan_every_hours: int = Field(default=24, alias="FULL_SCAN_EVERY_HOURS")

    # ---------------- Initial sync ----------------
    initial_sync: bool = Field(default=True, alias="INITIAL_SYNC")
    initial_sync_mark_processed: bool = Field(default=True, alias="INITIAL_SYNC_MARK_PROCESSED")
    initial_sync_max_reviews: int = Field(default=200, alias="INITIAL_SYNC_MAX_REVIEWS")
    # A review the dealership has already answered on Google needs no reply, so
    # it is stored but not queued for A3. This mirrors A3's own rule
    # (lib/review-sync.js: `if (replied) return 'posted'`).
    skip_already_answered: bool = Field(default=True, alias="SKIP_ALREADY_ANSWERED")

    # ---------------- API security ----------------
    a3_api_key: str = Field(default="", alias="A3_API_KEY")

    # ---------------- Collector backend ----------------
    # gbp_api  -> official Google Business Profile API (free, reliable, needs OAuth)
    # playwright -> public Google Maps page rendering (no credentials, best effort)
    # auto     -> gbp_api when credentials present, else playwright
    collector_backend: str = Field(default="auto", alias="COLLECTOR_BACKEND")
    reviews_per_check: int = Field(default=20, alias="REVIEWS_PER_CHECK")
    collector_timeout_seconds: int = Field(default=90, alias="COLLECTOR_TIMEOUT_SECONDS")
    collector_max_retries: int = Field(default=2, alias="COLLECTOR_MAX_RETRIES")
    collector_retry_backoff_seconds: int = Field(default=5, alias="COLLECTOR_RETRY_BACKOFF_SECONDS")

    # ---------------- Google Business Profile API (OAuth) ----------------
    google_client_id: str = Field(default="", alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(default="", alias="GOOGLE_CLIENT_SECRET")
    google_refresh_token: str = Field(default="", alias="GOOGLE_REFRESH_TOKEN")

    # ---------------- Playwright backend ----------------
    playwright_headless: bool = Field(default=True, alias="PLAYWRIGHT_HEADLESS")
    playwright_locale: str = Field(default="en-US", alias="PLAYWRIGHT_LOCALE")
    playwright_timezone: str = Field(default="America/Chicago", alias="PLAYWRIGHT_TIMEZONE")
    playwright_user_data_dir: str = Field(default="", alias="PLAYWRIGHT_USER_DATA_DIR")
    playwright_scroll_rounds: int = Field(default=40, alias="PLAYWRIGHT_SCROLL_ROUNDS")
    playwright_warm_session: bool = Field(default=True, alias="PLAYWRIGHT_WARM_SESSION")
    # Playwright's headless build advertises itself as "HeadlessChrome", and
    # Google serves that string a reviews-free listing almost always. Setting a
    # standard desktop Chrome UA is what makes public collection work at all.
    # See README "Google access reality check" -- this is a documented judgement
    # call, and setting it to "" restores Playwright's own UA.
    playwright_user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        ),
        alias="PLAYWRIGHT_USER_AGENT",
    )

    # ---------------- Email notifications ----------------
    notify_enabled: bool = Field(default=False, alias="NOTIFY_ENABLED")
    notify_on_check_now: bool = Field(default=True, alias="NOTIFY_ON_CHECK_NOW")
    notify_on_refresh: bool = Field(default=True, alias="NOTIFY_ON_REFRESH")
    notify_on_scheduled_check: bool = Field(default=False, alias="NOTIFY_ON_SCHEDULED_CHECK")
    notify_on_new_review: bool = Field(default=True, alias="NOTIFY_ON_NEW_REVIEW")
    # Alert when a dealership starts failing, and again when it recovers.
    # Sent on the transition only, never once per failed cycle.
    notify_on_failure: bool = Field(default=True, alias="NOTIFY_ON_FAILURE")
    notify_on_recovery: bool = Field(default=True, alias="NOTIFY_ON_RECOVERY")
    # One "still alive" message a day. Every other alert fires on an event, so
    # without this a quiet day and a dead process are the same empty inbox.
    notify_on_heartbeat: bool = Field(default=False, alias="NOTIFY_ON_HEARTBEAT")
    # Local hour, not UTC: it is read by a person over coffee. Sent on the first
    # check after this hour, so a machine woken late still sends it.
    heartbeat_hour: int = Field(default=8, alias="HEARTBEAT_HOUR")

    smtp_host: str = Field(default="smtp.gmail.com", alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_username: str = Field(default="", alias="SMTP_USERNAME")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    # Google OAuth instead of an App Password, for accounts whose domain blocks
    # 2-Step Verification. Set by scripts/connect_gmail_oauth.py at the project root.
    gmail_oauth_client_id: str = Field(default="", alias="GMAIL_OAUTH_CLIENT_ID")
    gmail_oauth_client_secret: str = Field(default="", alias="GMAIL_OAUTH_CLIENT_SECRET")
    gmail_oauth_refresh_token: str = Field(default="", alias="GMAIL_OAUTH_REFRESH_TOKEN")
    # Gmail requires STARTTLS on 587 (or implicit TLS on 465). Only turn this
    # off for a plaintext relay on localhost.
    smtp_use_tls: bool = Field(default=True, alias="SMTP_USE_TLS")

    notify_from: str = Field(default="", alias="NOTIFY_FROM")
    notify_from_name: str = Field(default="A3 Review Collector", alias="NOTIFY_FROM_NAME")
    notify_to: str = Field(default="", alias="NOTIFY_TO")
    notify_subject_prefix: str = Field(default="[A3 Collector]", alias="NOTIFY_SUBJECT_PREFIX")
    notify_dashboard_url: str = Field(
        default="http://127.0.0.1:8080/", alias="NOTIFY_DASHBOARD_URL"
    )
    # A Cloudflare quick tunnel gets a fresh hostname each time it starts, so the
    # public URL is written to a file by scripts/start_tunnel.sh rather than
    # baked into .env. Emails read it at send time, so a link is never stale.
    public_url_file: str = Field(
        default=str(BASE_DIR / "data" / "public_url.txt"), alias="PUBLIC_URL_FILE"
    )

    tunnel_log_file: str = Field(
        default=str(BASE_DIR / "logs" / "tunnel.log"), alias="TUNNEL_LOG_FILE"
    )

    def dashboard_url(self) -> str:
        """Public URL when a tunnel is up, otherwise the configured local one.

        A Cloudflare quick tunnel is assigned a fresh hostname every time it
        starts, so the live value is read at send time rather than stored in
        .env. An explicit public_url_file wins; otherwise the most recent
        hostname cloudflared logged is used.
        """
        try:
            path = Path(self.public_url_file)
            if path.exists():
                url = path.read_text().strip()
                if url.startswith("http"):
                    return url
        except OSError:
            pass

        try:
            log = Path(self.tunnel_log_file)
            if log.exists():
                found = re.findall(
                    r"https://[a-z0-9-]+\.trycloudflare\.com", log.read_text(errors="replace")
                )
                if found:
                    return found[-1]
        except OSError:
            pass

        return self.notify_dashboard_url
    # A burst of identical mail is what gets a sender filtered into Spam, so
    # repeat notifications of the same event are suppressed inside this window.
    notify_min_interval_seconds: int = Field(default=60, alias="NOTIFY_MIN_INTERVAL_SECONDS")
    # A review is emailed exactly once, and only while it is still recent. The
    # window stops a large backfill of historical reviews from arriving as a
    # wall of "new" mail.
    notify_max_age_hours: int = Field(default=24, alias="NOTIFY_MAX_AGE_HOURS")
    notify_timeout_seconds: int = Field(default=30, alias="NOTIFY_TIMEOUT_SECONDS")
    notify_max_retries: int = Field(default=2, alias="NOTIFY_MAX_RETRIES")

    def resolved_notify_from(self) -> str:
        """Default the From address to the authenticated mailbox.

        Gmail rewrites a mismatched From anyway, and an aligned From is what
        keeps DKIM/DMARC happy -- which is what keeps this out of Spam.
        """
        return (self.notify_from or self.smtp_username).strip()

    def notify_recipients(self) -> List[str]:
        raw = self.notify_to or self.smtp_username
        return [addr.strip() for addr in raw.split(",") if addr.strip()]

    def notify_config_problem(self) -> Optional[str]:
        """Human-readable reason notifications cannot be sent, or None."""
        if not self.smtp_host:
            return "SMTP_HOST is not set."
        if not self.smtp_username:
            return "SMTP_USERNAME is not set (your Gmail address)."
        if not self.smtp_password and not self.gmail_oauth_refresh_token:
            return (
                "SMTP_PASSWORD is not set. With 2-Step Verification enabled this must be a "
                "16-character Google App Password, not your account password."
            )
        if not self.notify_recipients():
            return "NOTIFY_TO is not set (who should receive the notifications)."
        return None

    # ---------------- Stall watchdog ----------------
    # The failure that looks like success: Google serves a listing with the
    # reviews missing, checks keep reporting success with zero found, and
    # collection has silently stopped. Across the whole roster reviews arrive
    # constantly, so a long silence from everyone is not a quiet week.
    stall_alert_enabled: bool = Field(default=True, alias="STALL_ALERT_ENABLED")
    stall_alert_hours: int = Field(default=48, alias="STALL_ALERT_HOURS")

    # ---------------- A3 Review Responder sync ----------------
    # Pushes new reviews to A3's api/reviews-sync endpoint. Outbound only, so it
    # works from behind NAT -- A3 on Railway cannot reach this machine.
    a3_sync_enabled: bool = Field(default=False, alias="A3_SYNC_ENABLED")
    a3_sync_url: str = Field(default="", alias="A3_SYNC_URL")
    a3_sync_secret: str = Field(default="", alias="A3_SYNC_SECRET")
    a3_sync_batch_size: int = Field(default=100, alias="A3_SYNC_BATCH_SIZE")
    a3_sync_timeout_seconds: int = Field(default=45, alias="A3_SYNC_TIMEOUT_SECONDS")
    a3_sync_max_retries: int = Field(default=2, alias="A3_SYNC_MAX_RETRIES")
    # A3 maps our business keys onto its own dealer slugs.
    a3_dealer_bmw_fwb: str = Field(default="bmw-fwb", alias="A3_DEALER_BMW_FWB")
    a3_dealer_mb_fwb: str = Field(default="mb-fwb", alias="A3_DEALER_MB_FWB")
    # A3's UPDATE writes business_reply without COALESCE, so sending a null
    # reply would erase the reply text it already holds. Leave this off until
    # A3 protects that column (or until the GBP API gives us the real text).
    a3_sync_send_reply_state: bool = Field(default=False, alias="A3_SYNC_SEND_REPLY_STATE")

    def a3_dealer_id(self, business_key: str) -> Optional[str]:
        return dealer_slug_for(business_key, {
            "bmw_fwb": self.a3_dealer_bmw_fwb,
            "mb_fwb": self.a3_dealer_mb_fwb,
        })

    def a3_sync_problem(self) -> Optional[str]:
        if not self.a3_sync_url:
            return "A3_SYNC_URL is not set."
        if not self.a3_sync_secret:
            return "A3_SYNC_SECRET is not set."
        if len(self.a3_sync_secret) < 32:
            return (
                "A3_SYNC_SECRET must be at least 32 characters -- A3 refuses every "
                f"request with a shorter secret (got {len(self.a3_sync_secret)})."
            )
        return None

    # ---------------- Google Reviews Manager (Railway) ----------------
    # The manager's own Google fetch needs Business Profile API approval, which
    # has not come through -- so the collector supplies its reviews instead.
    grm_enabled: bool = Field(default=False, alias="GRM_ENABLED")
    grm_url: str = Field(default="", alias="GRM_URL")
    grm_ingest_secret: str = Field(default="", alias="GRM_INGEST_SECRET")
    grm_timeout_seconds: int = Field(default=60, alias="GRM_TIMEOUT_SECONDS")
    grm_max_retries: int = Field(default=2, alias="GRM_MAX_RETRIES")
    grm_max_reviews: int = Field(default=1000, alias="GRM_MAX_REVIEWS")
    grm_dealer_bmw_fwb: str = Field(default="bmw-fwb", alias="GRM_DEALER_BMW_FWB")
    grm_dealer_mb_fwb: str = Field(default="mb-fwb", alias="GRM_DEALER_MB_FWB")

    def grm_dealer_slug(self, business_key: str) -> Optional[str]:
        return dealer_slug_for(business_key, {
            "bmw_fwb": self.grm_dealer_bmw_fwb,
            "mb_fwb": self.grm_dealer_mb_fwb,
        })

    def grm_problem(self) -> Optional[str]:
        if not self.grm_url:
            return "GRM_URL is not set."
        if not self.grm_ingest_secret:
            return "GRM_INGEST_SECRET is not set."
        if len(self.grm_ingest_secret) < 32:
            return (
                "GRM_INGEST_SECRET must be at least 32 characters -- the reviews manager "
                f"refuses anything shorter (got {len(self.grm_ingest_secret)})."
            )
        return None

    # ---------------- Monitored businesses ----------------
    bmw_fwb_name: str = Field(default="BMW of Fort Walton Beach", alias="BMW_FWB_NAME")
    bmw_fwb_google_url: str = Field(default="", alias="BMW_FWB_GOOGLE_URL")
    bmw_fwb_place_id: str = Field(default="", alias="BMW_FWB_PLACE_ID")
    bmw_fwb_gbp_location: str = Field(default="", alias="BMW_FWB_GBP_LOCATION")

    mb_fwb_name: str = Field(default="Mercedes-Benz of Fort Walton Beach", alias="MB_FWB_NAME")
    mb_fwb_google_url: str = Field(default="", alias="MB_FWB_GOOGLE_URL")
    mb_fwb_place_id: str = Field(default="", alias="MB_FWB_PLACE_ID")
    mb_fwb_gbp_location: str = Field(default="", alias="MB_FWB_GBP_LOCATION")

    # ---------------- Derived ----------------
    def businesses(self) -> List[BusinessConfig]:
        """ONLY the two dealerships seeded from .env -- never "all clients".

        Collection enumerates the Business table instead, so this has been a
        subset ever since the third dealership was added at /admin. For every
        active dealership use
        app.services.business_registry.active_business_configs().
        """
        return [
            BusinessConfig(
                key="bmw_fwb",
                name=self.bmw_fwb_name,
                google_url=self.bmw_fwb_google_url,
                place_id=self.bmw_fwb_place_id,
                gbp_location_name=self.bmw_fwb_gbp_location,
            ),
            BusinessConfig(
                key="mb_fwb",
                name=self.mb_fwb_name,
                google_url=self.mb_fwb_google_url,
                place_id=self.mb_fwb_place_id,
                gbp_location_name=self.mb_fwb_gbp_location,
            ),
        ]

    def has_gbp_credentials(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.google_refresh_token)

    def sqlite_path(self) -> Optional[Path]:
        if self.database_url.startswith("sqlite:///"):
            return Path(self.database_url[len("sqlite:///") :])
        return None


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # Make sure the directories the app writes to exist before anything runs.
    db_path = settings.sqlite_path()
    if db_path is not None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    Path(settings.log_file).parent.mkdir(parents=True, exist_ok=True)
    return settings


def reset_settings_cache() -> None:
    """Used by tests that need to reload configuration."""
    get_settings.cache_clear()
