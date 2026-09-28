"""Orchestrates one collection cycle.

Key guarantee (requirement 3): each dealership is processed inside its own
try/except and its own database transaction, so a failure on one can never stop
the other or abort the cycle.
"""
from __future__ import annotations

import datetime as dt
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from sqlalchemy.exc import IntegrityError

from app.collector.base import (
    CollectionResult,
    CollectorError,
    BackendUnavailable,
)
from app.collector.deduplication import partition_new
from app.collector.normalizer import to_review_row
from app.collector.registry import get_backends
from app.config import Settings, get_settings
from app.database.database import session_scope, sync_businesses_from_config
from app.services.cadence import plan_for
from app.services.watchdog import check_and_alert
from app.database.models import Business, CheckRun, Review, iso_utc, utcnow
from app.services import notifier, stats

logger = logging.getLogger("collector")

# Only one cycle at a time. A manual "Check Now" while the scheduler is running
# would otherwise double-collect and waste Google quota.
_cycle_lock = threading.Lock()

# One lock per dealership, so the same Google listing is never scraped by two
# threads at once.
#
# _cycle_lock stops two CYCLES overlapping, but onboarding deliberately runs
# outside it (a first pull takes minutes and must not stall every paced check
# behind one new arrival). The consequence, seen on Merit Auto Group on
# 2026-09-08: its onboarding pull started at 06:43:32 and the scheduled cycle
# started a second pull of the same listing at 06:45:05, so the listing was
# fetched twice, about six minutes of browser each, for ten extra reviews.
# Deduplication meant no bad data -- it was purely wasted capacity, which is
# exactly the budget that decides how many dealerships can be served.
_business_locks: Dict[int, threading.Lock] = {}
_business_locks_guard = threading.Lock()


def _lock_for(business_id: int) -> threading.Lock:
    with _business_locks_guard:
        return _business_locks.setdefault(business_id, threading.Lock())


class CollectionService:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()

    # ==================================================================
    # Public entry points
    # ==================================================================
    def run_cycle(self, trigger: str = "scheduled") -> Dict:
        """Check every active business. Never raises."""
        if not _cycle_lock.acquire(blocking=False):
            logger.info("A collection cycle is already running; skipping this %s trigger.", trigger)
            return {"skipped": True, "reason": "A collection cycle is already in progress.", "results": []}

        started = utcnow()
        try:
            # Pick up dealerships added at the Responder's /admin screen. This
            # ran only at startup, so a new customer collected nothing until
            # somebody happened to restart the collector.
            try:
                sync_businesses_from_config()
            except Exception:  # pragma: no cover - never break a cycle over this
                logger.exception("Could not refresh the dealership list")

            with session_scope() as session:
                businesses = session.query(Business).filter_by(active=True).order_by(Business.id).all()
                # Each dealership is paced by how often it actually receives
                # reviews. A quiet one is not checked 96 times to find nothing;
                # that budget goes to serving more dealerships instead.
                targets, waiting = [], []
                for b in businesses:
                    plan = plan_for(session, b, self.settings)
                    if plan["due"]:
                        targets.append((b.id, b.name))
                    else:
                        waiting.append((b.name, plan))

            if not targets and not waiting:
                logger.warning(
                    "No active businesses configured. Add one at the Responder's /admin "
                    "screen, or set the place id in .env."
                )
                return {"skipped": False, "results": [], "started_at": iso_utc(started)}

            if not targets:
                # Everything is on file and none of it is due yet. That is the
                # pacing working, not a problem, and it must not read like one.
                logger.info(
                    "Nothing due this cycle; %d dealership(s) are waiting for their next slot.",
                    len(waiting),
                )
                for name, plan in waiting:
                    logger.debug("%s: %s", name, plan["reason"])
                return {"skipped": False, "results": [], "started_at": iso_utc(started)}

            for name, plan in waiting:
                logger.info("%s: not due, %s", name, plan["reason"])

            logger.info(
                "Review check cycle started (%s) for %d of %d business(es)",
                trigger, len(targets), len(targets) + len(waiting),
            )
            results = self._check_all(targets, trigger)

            self._sync_to_a3()
            self._sync_to_manager()

            # Checked after the work, so it sees this cycle's results. Never
            # allowed to break a cycle: an alarm that can take the system down
            # is worse than no alarm.
            try:
                with session_scope() as session:
                    check_and_alert(notifier, session, self.settings)
            except Exception:  # pragma: no cover
                logger.exception("Stall watchdog failed")

            total_new = sum(r.get("new_reviews", 0) for r in results)
            failed = [r["business_name"] for r in results if r.get("status") == "failed"]
            logger.info(
                "Review check cycle complete: %d new review(s); %d/%d business(es) succeeded%s",
                total_new,
                len(results) - len(failed),
                len(results),
                f"; failed: {', '.join(failed)}" if failed else "",
            )
            return {
                "skipped": False,
                "trigger": trigger,
                "started_at": iso_utc(started),
                "finished_at": iso_utc(utcnow()),
                "total_new_reviews": total_new,
                "results": results,
            }
        finally:
            _cycle_lock.release()

    # ==================================================================
    def onboard_new(self) -> Dict:
        """Collect for any dealership we have never collected for.

        Someone adds a customer at /admin and reasonably expects to see reviews,
        not a fifteen minute wait in front of an empty queue with nothing saying
        a check is coming. This runs on a short timer, does nothing at all in the
        normal case, and picks up a new dealership within a minute of it
        appearing.

        Only ever touches dealerships with no successful check on record. An
        established one is left entirely to the ordinary paced cycle.
        """
        try:
            sync_businesses_from_config()
        except Exception:
            logger.exception("Could not refresh the dealership list")
            return {"onboarded": []}

        with session_scope() as session:
            checked = {
                row.business_id for row in
                session.query(CheckRun).filter(CheckRun.status == "success").all()
            }
            fresh = [
                (b.id, b.name)
                for b in session.query(Business).filter_by(active=True).order_by(Business.id).all()
                if b.id not in checked
            ]

        if not fresh:
            return {"onboarded": []}

        # Not inside the cycle lock on purpose: a first collection can take
        # minutes, and holding the lock would stall the paced checks for every
        # established dealership behind one new arrival.
        logger.info(
            "New dealership(s) to collect for the first time: %s",
            ", ".join(name for _, name in fresh),
        )
        results = [self._check_one(bid, name, "onboarding") for bid, name in fresh]

        # Push straight away, so the dashboard fills in as soon as the pull is
        # done rather than at the end of the next cycle.
        self._sync_to_manager()
        self._sync_to_a3()
        return {"onboarded": [r.get("business_name") for r in results]}

    def _check_one(self, business_id: int, name: str, trigger: str, *,
                   jitter: float = 0.0) -> Dict:
        """One dealership, fully isolated. Never raises."""
        if jitter:
            # Workers otherwise all start at the same instant and fire their page
            # loads together, which is the most conspicuous possible pattern.
            time.sleep(random.uniform(0, jitter))

        # Every trigger -- scheduled, onboarding, manual -- funnels through here,
        # so this is the one place that can see a second check of the same
        # dealership starting while the first is still running. Skipped rather
        # than queued: by the time the running pull finishes it has already
        # collected whatever this one was going to look for.
        lock = _lock_for(business_id)
        if not lock.acquire(blocking=False):
            logger.info(
                "%s is already being checked; skipping this %s trigger.", name, trigger
            )
            return {
                "business_name": name,
                "business_id": business_id,
                "status": "skipped",
                "reason": "a check of this dealership is already running",
                "reviews_found": 0,
                "new_reviews": 0,
            }

        try:
            return self.check_business(business_id, trigger=trigger)
        except Exception as exc:  # pragma: no cover - last-resort guard
            logger.exception("Unhandled error while checking %s: %s", name, exc)
            return {
                "business_name": name,
                "business_id": business_id,
                "status": "failed",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "will_retry": True,
                "reviews_found": 0,
                "new_reviews": 0,
            }
        finally:
            lock.release()

    def _check_all(self, targets: List[tuple], trigger: str) -> List[Dict]:
        """Check the due dealerships, in parallel when configured.

        Results come back in the order the dealerships were listed regardless of
        which finished first, so the emails and the dashboard read the same way
        they always did.
        """
        workers = max(1, min(8, self.settings.collector_workers))

        if workers == 1 or len(targets) <= 1:
            return [self._check_one(bid, name, trigger) for bid, name in targets]

        logger.info("Checking %d dealership(s) across %d worker(s)", len(targets), workers)
        results: List[Optional[Dict]] = [None] * len(targets)
        jitter = self.settings.collector_worker_jitter_seconds

        # Each thread gets its own Playwright instance and its own SQLite
        # connection. The database is in WAL mode with a 10 second busy timeout,
        # which is what lets several writers run without tripping over each other.
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="collect") as pool:
            futures = {
                pool.submit(self._check_one, bid, name, trigger, jitter=jitter): index
                for index, (bid, name) in enumerate(targets)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()

        return [r for r in results if r is not None]

    def check_business(self, business_id: int, trigger: str = "scheduled") -> Dict:
        """Run one business end to end and record a CheckRun row."""
        with session_scope() as session:
            business = session.get(Business, business_id)
            if business is None:
                raise ValueError(f"Business id {business_id} not found")
            name = business.name
            key = business.key
            first_sync = not business.initial_sync_done
            previous = (
                session.query(CheckRun)
                .filter(CheckRun.business_id == business.id,
                        CheckRun.status.in_(("success", "failed")))
                .order_by(CheckRun.started_at.desc())
                .first()
            )
            previous_status = previous.status if previous else None
            run = CheckRun(business_id=business.id, trigger=trigger, status="running")
            session.add(run)
            session.flush()
            run_id = run.id

        start = time.monotonic()
        logger.info("%s: check started", name)

        limit = (
            self.settings.initial_sync_max_reviews
            if (first_sync and self.settings.initial_sync)
            else self.settings.reviews_per_check
        )

        # What the listing reported last time. The backend compares it with the
        # live figure and stops early when nothing has changed. Withheld on a
        # first sync, and periodically, so a full read still happens.
        known_count = None
        if self.settings.fast_path_enabled and not first_sync:
            with session_scope() as session:
                fresh = session.get(Business, business_id)
                plan = plan_for(session, fresh, self.settings)
                if not plan["full_scan"]:
                    known_count = fresh.lifetime_reviews

        try:
            result = self._collect_with_retries(business_id, limit, known_count)
        except CollectorError as exc:
            return self._record_failure(run_id, name, exc, start,
                                        previous_status=previous_status)
        except Exception as exc:  # pragma: no cover - unexpected
            return self._record_failure(run_id, name, exc, start, retryable=True,
                                        previous_status=previous_status)

        # ---- store ----
        try:
            stored = self._store(business_id, key, result, first_sync=first_sync)
        except Exception as exc:
            logger.exception("%s: collected %d review(s) but storing failed", name, len(result.reviews))
            return self._record_failure(run_id, name, exc, start, retryable=True,
                                        previous_status=previous_status)

        duration_ms = int((time.monotonic() - start) * 1000)
        found = len(result.reviews)
        new_count = stored["new"]
        skipped = stored["skipped"]

        if result.skipped_unchanged:
            logger.info("%s: unchanged, stopped before reading the reviews", name)
        else:
            logger.info("%s: %d review(s) found", name, found)
        if stored.get("updated"):
            logger.info("%s: %d existing review(s) refreshed", name, stored["updated"])
        logger.info(
            "%s: %d new review(s), %d existing review(s) skipped%s",
            name,
            new_count,
            skipped,
            " [initial sync]" if first_sync and self.settings.initial_sync else "",
        )
        for note in result.notes:
            logger.warning("%s: %s", name, note)
        logger.info("%s: check complete in %dms via %s", name, duration_ms, result.backend)

        # The dealership was configured with a search URL or a bare name, and
        # the backend worked out which listing is meant. Kept here, in the
        # collector's own record, and nowhere else.
        #
        # An earlier version reported this back and rewrote the dealership in the
        # Responder, which overwrote what somebody had typed at /admin. The
        # Responder owns what a dealership IS; this service only collects its
        # reviews. The resolved listing reaches the dashboard as part of the
        # review push instead, as data rather than as an edit.
        if result.resolved_url:
            with session_scope() as session:
                fresh = session.get(Business, business_id)
                fresh.google_url = result.resolved_url
            logger.info(
                "%s: collecting from the listing for '%s'",
                name, result.resolved_name or result.resolved_url,
            )

        if result.average_rating or result.total_review_count:
            with session_scope() as session:
                business = session.get(Business, business_id)
                if result.average_rating:
                    business.overall_rating = result.average_rating
                if result.total_review_count:
                    business.lifetime_reviews = result.total_review_count

        with session_scope() as session:
            run = session.get(CheckRun, run_id)
            run.status = "success"
            run.backend = result.backend
            run.finished_at = utcnow()
            run.duration_ms = duration_ms
            run.reviews_found = found
            run.new_reviews = new_count
            run.skipped_existing = skipped
            run.will_retry = False
            run.fast_path = bool(result.skipped_unchanged)

        # --- alerts -------------------------------------------------------
        # These are what make the service useful when nobody is watching the
        # dashboard. Never allowed to break a check.
        try:
            if new_count > 0 and not (first_sync and self.settings.initial_sync
                                      and self.settings.initial_sync_mark_processed):
                fresh_ids = [r["review_id"] for r in stored["reviews"]]
                notifier.send_event(
                    "new_review",
                    results=[{"business_name": name, "status": "success",
                              "reviews_found": found, "new_reviews": new_count,
                              "skipped_existing": skipped}],
                    totals=stats.global_stats(),
                    reviews=stored["reviews"],
                    on_delivered=lambda ids=fresh_ids: stats.mark_notified(ids),
                )
            if previous_status == "failed":
                notifier.send_event(
                    "recovered",
                    results=[{"business_name": name, "status": "success",
                              "reviews_found": found, "new_reviews": new_count,
                              "skipped_existing": skipped}],
                    totals=stats.global_stats(),
                    reviews=stats.pending_reviews(20),
                )
        except Exception:  # pragma: no cover - alerting must never break a check
            logger.exception("%s: alerting failed after a successful check", name)

        return {
            "business_name": name,
            "business_id": business_id,
            "status": "success",
            "backend": result.backend,
            "reviews_found": found,
            "new_reviews": new_count,
            "skipped_existing": skipped,
            "updated_existing": stored.get("updated", 0),
            "duration_ms": duration_ms,
            "initial_sync": bool(first_sync and self.settings.initial_sync),
        }

    # ==================================================================
    # Internals
    # ==================================================================
    def _collect_with_retries(self, business_id: int, limit: int,
                              known_review_count: Optional[int] = None) -> CollectionResult:
        """Try each configured backend, retrying transient failures."""
        backends = get_backends(self.settings)
        if not backends:
            raise BackendUnavailable("No collector backend is configured.")

        last_error: Optional[Exception] = None
        for backend in backends:
            available, reason = backend.is_available()
            if not available:
                logger.warning("Backend '%s' unavailable: %s", backend.name, reason)
                last_error = BackendUnavailable(f"[{backend.name}] {reason}")
                continue

            attempts = max(1, self.settings.collector_max_retries + 1)
            for attempt in range(1, attempts + 1):
                with session_scope() as session:
                    business = session.get(Business, business_id)
                    session.expunge(business)
                try:
                    return backend.collect(business, limit, known_review_count)
                except CollectorError as exc:
                    last_error = exc
                    if not exc.retryable or attempt >= attempts:
                        logger.warning(
                            "Backend '%s' failed (attempt %d/%d, not retrying in this cycle): %s",
                            backend.name, attempt, attempts, exc,
                        )
                        break
                    delay = self.settings.collector_retry_backoff_seconds * attempt
                    logger.warning(
                        "Backend '%s' failed (attempt %d/%d): %s -- retrying in %ds",
                        backend.name, attempt, attempts, exc, delay,
                    )
                    time.sleep(delay)
                except Exception as exc:  # pragma: no cover - defensive
                    last_error = exc
                    logger.exception("Backend '%s' raised an unexpected error", backend.name)
                    break

        raise last_error if last_error else BackendUnavailable("No backend produced a result.")

    # ------------------------------------------------------------------
    def _store(self, business_id: int, business_key: str, result: CollectionResult, *, first_sync: bool) -> Dict[str, int]:
        """Insert only genuinely new reviews. Idempotent."""
        mark_processed = (
            first_sync and self.settings.initial_sync and self.settings.initial_sync_mark_processed
        )

        new_count = 0
        skipped = 0
        stored_payloads: List[Dict] = []
        with session_scope() as session:
            business = session.get(Business, business_id)
            new_items, seen_items = partition_new(session, business_key, result.reviews)
            skipped = len(seen_items)

            # Reviews we already hold are not immutable: a customer can edit the
            # text or the rating, and the dealership can post a reply days later.
            # Refresh those fields so A3 is told the truth, and so an edit from
            # 5 stars to 1 star cannot go unnoticed.
            refreshed = self._refresh_existing(session, seen_items)

            for review_id, is_native, raw in new_items:
                already_answered = (
                    self.settings.skip_already_answered and bool(raw.owner_replied)
                )
                row = to_review_row(
                    business, review_id, is_native, raw,
                    processed=mark_processed or already_answered,
                )
                session.add(row)
                try:
                    # Flush per review: the UNIQUE constraint is the final
                    # backstop, and one loser must not roll back the winners.
                    session.flush()
                    new_count += 1
                    stored_payloads.append(row.to_a3_payload())
                except IntegrityError:
                    session.rollback()
                    skipped += 1
                    logger.debug("Duplicate blocked by UNIQUE constraint: %s", review_id)

            if first_sync:
                business = session.get(Business, business_id)
                business.initial_sync_done = True

        return {
            "new": new_count,
            "skipped": skipped,
            "updated": refreshed,
            "reviews": stored_payloads,
        }

    # ------------------------------------------------------------------
    def _sync_to_a3(self) -> None:
        """Hand anything A3 has not accepted to the A3 Review Responder.

        Runs after every cycle -- including scheduled ones -- so A3 is fed
        without anybody clicking. Never allowed to break a collection run.
        """
        if not self.settings.a3_sync_enabled:
            return

        from app.services import a3_sync

        try:
            pending = stats.unsynced_reviews(limit=self.settings.a3_sync_batch_size)
            if not pending:
                return
            outcome = a3_sync.push(pending, settings=self.settings)
            stamped = stats.mark_a3_synced(outcome["accepted"])
            logger.info(
                "A3 sync: %d sent, %d accepted, %d skipped",
                outcome["sent"], stamped, outcome["skipped"],
            )
        except a3_sync.A3NotConfigured as exc:
            logger.error("A3 sync is enabled but not configured: %s", exc)
        except Exception as exc:
            logger.error("A3 sync failed (reviews are safe and will retry): %s", exc)

    # ------------------------------------------------------------------
    def _sync_to_manager(self) -> None:
        """Push every collected review to the Google Reviews Manager.

        The manager's own Google fetch needs Business Profile API approval that
        has not come through, so this is currently its only real data. Sends the
        whole set each cycle rather than a delta: the manager merges on the
        Google review id, so re-sending is a no-op, and it means a manager that
        lost its disk refills itself on the next check.

        Never allowed to break a collection run.
        """
        if not self.settings.grm_enabled:
            return

        from app.services import grm_sync

        try:
            with session_scope() as session:
                businesses = session.query(Business).filter_by(active=True).all()
                targets = [
                    (b.id, b.key, b.name, b.place_id, b.overall_rating,
                     b.lifetime_reviews, b.google_url)
                    for b in businesses
                ]

            failures: List[str] = []
            for business_id, key, name, place_id, overall, lifetime, google_url in targets:
                slug = self.settings.grm_dealer_slug(key)
                if not slug:
                    logger.warning("No reviews-manager dealer slug configured for %s", key)
                    continue

                with session_scope() as session:
                    rows = (
                        session.query(Review)
                        .filter(Review.business_id == business_id)
                        .order_by(Review.review_date.desc().nullslast())
                        .limit(self.settings.grm_max_reviews)
                        .all()
                    )
                    payload = [row.to_a3_payload() for row in rows]

                if not payload:
                    continue

                # Each dealership isolated, exactly as the checks are. One
                # dealership the manager does not recognise used to abort the
                # whole sync, so every dealership after it in the list silently
                # went unsynced: reviews collected, stored, and never shown.
                try:
                    outcome = grm_sync.push_dealer(
                        slug, payload,
                        {
                            "placeId": place_id,
                            "totalScore": overall,
                            "reviewsCount": lifetime,
                            # The listing these reviews actually came from. Sent
                            # as part of the data so the dashboard can link to
                            # it, rather than the collector reaching over and
                            # editing the dealership's record.
                            "url": google_url
                            or (f"https://www.google.com/maps/place/?q=place_id:{place_id}"
                                if place_id else None),
                        },
                        settings=self.settings,
                    )
                except Exception as exc:
                    failures.append(name)
                    logger.error(
                        "%s -> reviews manager FAILED (its reviews are safe and will "
                        "retry; other dealerships continue): %s", name, exc,
                    )
                    continue

                logger.info(
                    "%s -> reviews manager: %d added, %d updated, %d total",
                    name, outcome.get("reviews_added", 0),
                    outcome.get("reviews_updated", 0), outcome.get("reviews_total", 0),
                )
            if failures:
                logger.warning(
                    "Reviews-manager sync finished with %d dealership(s) unsynced: %s",
                    len(failures), ", ".join(failures),
                )
        except grm_sync.GRMNotConfigured as exc:
            logger.error("Reviews-manager sync is enabled but not configured: %s", exc)
        except Exception as exc:
            logger.error("Reviews-manager sync failed (reviews are safe, will retry): %s", exc)

    # ------------------------------------------------------------------
    @staticmethod
    def _refresh_existing(session, seen_items) -> int:
        """Update mutable fields on reviews we already store.

        Deliberately conservative: a shorter freshly-read comment never replaces
        a longer stored one, because a failed "See more" expansion would
        otherwise silently truncate real customer text.
        """
        updated = 0
        for review_id, _is_native, raw in seen_items:
            row = session.query(Review).filter_by(review_id=review_id).one_or_none()
            if row is None:
                continue

            changed = []

            if raw.owner_replied and not row.owner_replied:
                row.owner_replied = True
                changed.append("owner replied")
                # Answered on Google -> nothing left for A3 to draft.
                if not row.processed:
                    row.processed = True
                    row.processed_at = utcnow()
                    changed.append("closed as already answered")
            if raw.owner_reply and raw.owner_reply != (row.owner_reply or ""):
                row.owner_reply = raw.owner_reply[:20000]
                changed.append("reply text")
            if raw.owner_reply_date and not row.owner_reply_date:
                row.owner_reply_date = raw.owner_reply_date

            fresh_text = raw.review_text or ""
            if fresh_text and len(fresh_text) > len(row.review_text or ""):
                row.review_text = fresh_text[:20000]
                changed.append("comment")

            if raw.rating is not None and 1 <= raw.rating <= 5 and row.rating != raw.rating:
                logger.warning(
                    "Review %s rating changed %s -> %s (customer edited it)",
                    review_id, row.rating, raw.rating,
                )
                row.rating = raw.rating
                changed.append("rating")

            if changed:
                updated += 1
                logger.info("Refreshed %s: %s", review_id, ", ".join(changed))
        return updated

    # ------------------------------------------------------------------
    def _record_failure(self, run_id: int, name: str, exc: Exception, start: float,
                        retryable: Optional[bool] = None,
                        previous_status: Optional[str] = None) -> Dict:
        duration_ms = int((time.monotonic() - start) * 1000)
        will_retry = getattr(exc, "retryable", True) if retryable is None else retryable
        interval = self.settings.check_interval_minutes

        logger.error(
            "%s: check FAILED after %dms | operation=collect | cause=%s: %s | %s",
            name,
            duration_ms,
            type(exc).__name__,
            exc,
            (
                f"will retry automatically at the next scheduled check (in ~{interval} min)"
                if will_retry
                else "will NOT retry automatically -- this needs a configuration fix"
            ),
        )

        with session_scope() as session:
            run = session.get(CheckRun, run_id)
            if run is not None:
                run.status = "failed"
                run.finished_at = utcnow()
                run.duration_ms = duration_ms
                run.error_type = type(exc).__name__
                run.error_message = str(exc)[:2000]
                run.will_retry = will_retry
            business_id = run.business_id if run is not None else None

        # Alert on the transition into failure only. Alerting every 15 minutes
        # would train everyone to ignore it.
        if previous_status != "failed":
            try:
                notifier.send_event(
                    "failure",
                    results=[{
                        "business_name": name,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc)[:500],
                    }],
                    totals=stats.global_stats(),
                    reviews=[],
                )
            except Exception:  # pragma: no cover
                logger.exception("%s: could not send the failure alert", name)
        else:
            logger.info("%s: still failing; alert already sent, not repeating", name)

        return {
            "business_name": name,
            "business_id": business_id,
            "status": "failed",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "will_retry": will_retry,
            "reviews_found": 0,
            "new_reviews": 0,
            "duration_ms": duration_ms,
        }
