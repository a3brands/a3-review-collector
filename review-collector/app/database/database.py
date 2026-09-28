"""SQLite engine + session management.

WAL journalling lets the scheduler write while the API reads, which is exactly
the access pattern here (background collection + A3 polling the REST API).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.database.models import Base, Business

logger = logging.getLogger(__name__)

_engine: Optional[Engine] = None
_SessionFactory: Optional[sessionmaker] = None


def _configure_sqlite(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def get_engine() -> Engine:
    global _engine, _SessionFactory
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, echo=False, future=True, connect_args=connect_args)
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _configure_sqlite)
        _SessionFactory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
        logger.debug("Database engine created for %s", url)
    return _engine


def get_session_factory() -> sessionmaker:
    get_engine()
    assert _SessionFactory is not None
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Create tables if they do not exist, then apply column migrations.

    Safe to call on every startup and safe on a populated database.
    """
    Base.metadata.create_all(get_engine())
    _migrate_columns()


# Columns added after the first release. SQLite's ADD COLUMN is non-destructive
# and cannot fail on existing rows, so this is safe to run on every boot.
_ADDED_COLUMNS = {
    "businesses": [
        ("overall_rating", "REAL"),
        ("lifetime_reviews", "INTEGER"),
        ("feature_id", "TEXT"),
        ("latitude", "REAL"),
        ("longitude", "REAL"),
        ("configured_url", "TEXT"),
    ],
    "check_runs": [
        ("fast_path", "BOOLEAN NOT NULL DEFAULT 0"),
    ],
    "reviews": [
        ("owner_replied", "BOOLEAN NOT NULL DEFAULT 0"),
        ("owner_reply", "TEXT"),
        ("owner_reply_date", "DATETIME"),
        ("notified_at", "DATETIME"),
        ("a3_synced_at", "DATETIME"),
    ],
}


def _migrate_columns() -> None:
    from sqlalchemy import text

    engine = get_engine()
    with engine.begin() as connection:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {
                row[1] for row in connection.execute(text(f"PRAGMA table_info({table})"))
            }
            for name, definition in columns:
                if name in existing:
                    continue
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
                logger.info("Migrated: added %s.%s", table, name)


def _is_listing_url(url: Optional[str]) -> bool:
    """A link that opens one Google listing, as opposed to a search.

    Mirrors GoogleMapsBackend.looks_like_a_listing.
    """
    return bool(url) and ("/maps/place/" in url or "place_id:" in url)


def sync_businesses_from_config() -> None:
    """Upsert the known dealerships into the businesses table.

    The Responder's /admin screen is the source of truth for identity (place_id
    / URL); .env remains the fallback for anything it does not list, and for
    when the Responder cannot be reached. Editing either and restarting is
    enough to re-point the collector.

    Deliberately additive: a dealership that disappears from the list is left
    alone rather than deactivated. A momentary bad answer from the Responder
    must never silently stop collecting for a paying customer.
    """
    from app.collector.remote_registry import keys_listed_remotely, resolve_businesses

    settings = get_settings()
    listed = keys_listed_remotely(settings)

    with session_scope() as session:
        # A dealership the Responder answered without is one somebody switched
        # off there. Checking it anyway burns a slot every cycle and holds
        # health at degraded forever. Only ever acted on when the Responder
        # actually answered: `listed` is None when it could not be reached.
        if listed is not None:
            for row in session.query(Business).filter_by(active=True).all():
                if row.key not in listed and row.key not in {b.key for b in settings.businesses()}:
                    row.active = False
                    logger.info(
                        "Deactivated %s (%s): the Responder no longer lists it",
                        row.name, row.key,
                    )

        for cfg in resolve_businesses(settings):
            row = session.query(Business).filter_by(key=cfg.key).one_or_none()
            if row is None:
                row = Business(key=cfg.key, name=cfg.name)
                session.add(row)
                logger.info("Registered business %s (%s)", cfg.name, cfg.key)
            row.name = cfg.name
            # Do not throw away a listing the collector resolved.
            #
            # A dealership configured with a search link rather than a listing
            # (Merit Auto Group, from /admin) makes the backend search Maps and
            # save the listing it lands on into google_url. This sync runs on
            # every cycle and used to overwrite that with the search link again,
            # so every check searched afresh -- and Merit, a group with several
            # locations, got "several possible businesses" and failed 4 times
            # between 2026-09-11 and 2026-09-14, ~4.5 minutes each.
            #
            # Replaced when the configured link actually changes, or when what is
            # configured is itself a listing: /admin stays the source of truth.
            incoming = cfg.google_url
            keeps_resolved = (
                incoming == row.configured_url
                and _is_listing_url(row.google_url)
                and not _is_listing_url(incoming)
            )
            if not keeps_resolved:
                row.google_url = incoming
            row.configured_url = incoming
            row.place_id = cfg.place_id
            row.gbp_location_name = cfg.gbp_location_name
            row.active = cfg.is_configured()
            if not cfg.is_configured():
                logger.warning(
                    "Business %s (%s) has no PLACE_ID / GOOGLE_URL / GBP_LOCATION configured "
                    "-- it is marked inactive and will be skipped.",
                    cfg.name,
                    cfg.key,
                )


def close_orphaned_checks() -> None:
    """Finish off check runs that were interrupted by a restart.

    A process killed mid-check leaves its CheckRun row as 'running' forever,
    which would show the dealership as permanently "checking" on the dashboard.
    Nothing is retried here -- the row is simply closed honestly.
    """
    from app.database.models import CheckRun, utcnow

    with session_scope() as session:
        stale = session.query(CheckRun).filter(CheckRun.status == "running").all()
        for run in stale:
            run.status = "failed"
            run.finished_at = utcnow()
            run.error_type = "Interrupted"
            run.error_message = (
                "The collector was stopped or restarted while this check was running. "
                "No reviews were lost; the next scheduled check picks up normally."
            )
            run.will_retry = True
        if stale:
            logger.info("Closed %d interrupted check run(s) from a previous process", len(stale))


def reset_engine() -> None:
    """Drop cached engine/session factory (used by tests)."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
