"""In-process scheduler.

Runs inside the same process as the API, so deployment is a single command and
there is no external cron service to pay for. Collection runs on a thread pool
because the Playwright sync API and SQLite are both blocking.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Dict, Optional

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import Settings, get_settings
from app.database.models import iso_utc
from app.services.collection_service import CollectionService

logger = logging.getLogger("scheduler")

JOB_ID = "review_check"
ONBOARD_JOB_ID = "onboard_new_dealerships"
HEARTBEAT_JOB_ID = "daily_heartbeat"
# Checked often, sends at most once a day. Polling beats a daily cron here:
# this machine is off overnight, and a job pinned to one moment is a job
# that silently never runs.
HEARTBEAT_POLL_SECONDS = 900


class ReviewScheduler:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.service = CollectionService(self.settings)
        self._scheduler = BackgroundScheduler(
            executors={"default": ThreadPoolExecutor(max_workers=2)},
            job_defaults={
                "coalesce": True,      # a backlog collapses into one run
                "max_instances": 1,    # never overlap cycles
                "misfire_grace_time": 300,
            },
            timezone="UTC",
        )
        self.last_result: Optional[Dict] = None

    # ------------------------------------------------------------------
    def _job(self, trigger: str = "scheduled") -> None:
        try:
            self.last_result = self.service.run_cycle(trigger=trigger)
        except Exception:  # pragma: no cover - the service already guards
            logger.exception("Scheduled review check raised an unexpected error")

    def _heartbeat_job(self) -> None:
        """Cheap in the normal case: reads a marker file and returns."""
        try:
            from app.services import heartbeat
            heartbeat.run_if_due(self.settings)
        except Exception:  # pragma: no cover - a missed heartbeat must not stop collection
            logger.exception("Heartbeat check raised an unexpected error")

    def _onboard_job(self) -> None:
        """Cheap in the normal case: one call to the Responder and nothing else."""
        try:
            self.service.onboard_new()
        except Exception:  # pragma: no cover - never let this kill the scheduler
            logger.exception("Onboarding check raised an unexpected error")

    # ------------------------------------------------------------------
    def start(self) -> Dict:
        if self._scheduler.running:
            return self.status()

        self._scheduler.add_job(
            self._job,
            trigger=IntervalTrigger(minutes=self.settings.check_interval_minutes),
            id=JOB_ID,
            name="Google review check",
            replace_existing=True,
            next_run_time=dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(seconds=self.settings.startup_check_delay_seconds),
        )
        # A dealership added at /admin should not sit behind a fifteen minute
        # wait. This looks for one that has never been collected for, and does
        # nothing whenever there isn't one.
        if self.settings.onboard_poll_seconds > 0:
            self._scheduler.add_job(
                self._onboard_job,
                trigger=IntervalTrigger(seconds=self.settings.onboard_poll_seconds),
                id=ONBOARD_JOB_ID,
                name="Collect for new dealerships",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )

        if self.settings.notify_on_heartbeat:
            self._scheduler.add_job(
                self._heartbeat_job,
                trigger=IntervalTrigger(seconds=HEARTBEAT_POLL_SECONDS),
                id=HEARTBEAT_JOB_ID,
                name="Daily heartbeat",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                # First poll soon after start, not a full interval later. A
                # laptop opened at 08:02 and closed at 08:12 would otherwise
                # never reach the 08:17 first tick and never send that day.
                next_run_time=dt.datetime.now(dt.timezone.utc)
                + dt.timedelta(seconds=60),
            )

        self._scheduler.start()
        if self.settings.notify_on_heartbeat:
            logger.info(
                "Daily heartbeat enabled: sent on the first check after %02d:00 local",
                self.settings.heartbeat_hour,
            )
        logger.info(
            "Scheduler started: checking every %d minute(s); first check in %d second(s)",
            self.settings.check_interval_minutes,
            self.settings.startup_check_delay_seconds,
        )
        logger.info(
            "New dealerships are picked up within %d second(s) of being added",
            self.settings.onboard_poll_seconds,
        )
        return self.status()

    def stop(self) -> Dict:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")
        return self.status()

    def restart(self) -> Dict:
        self.stop()
        # A shut-down APScheduler instance cannot be restarted; build a fresh one.
        self._scheduler = BackgroundScheduler(
            executors={"default": ThreadPoolExecutor(max_workers=2)},
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
            timezone="UTC",
        )
        logger.info("Scheduler restarting")
        return self.start()

    # ------------------------------------------------------------------
    def trigger_now(self) -> Dict:
        """Synchronous manual check -- used by the dashboard's 'Check Now'."""
        logger.info("Manual check requested")
        self.last_result = self.service.run_cycle(trigger="manual")
        return self.last_result

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    def next_run_time(self) -> Optional[dt.datetime]:
        if not self._scheduler.running:
            return None
        job = self._scheduler.get_job(JOB_ID)
        return job.next_run_time if job else None

    def status(self) -> Dict:
        nxt = self.next_run_time()
        return {
            "running": self.running,
            "interval_minutes": self.settings.check_interval_minutes,
            "next_check": iso_utc(nxt),
            "job_id": JOB_ID,
        }


_scheduler: Optional[ReviewScheduler] = None


def get_scheduler() -> ReviewScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = ReviewScheduler()
    return _scheduler


def reset_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.stop()
    _scheduler = None
