"""Console + rotating-file logging in the format described in the README."""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from app.config import Settings

LOG_FORMAT = "[%(asctime)s] %(levelname)-7s %(name)-18s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


# Playwright tears its browser down by closing the pipe underneath asyncio, and
# asyncio then reports the in-flight tasks it abandoned at ERROR level. There is
# nothing to act on -- the collection itself has already finished and recorded
# its result -- but 332 of these had accumulated in the log, which is enough to
# bury a real failure. Matched narrowly on the teardown signature so a genuine
# asyncio error still comes through.
_TEARDOWN_SIGNATURES = (
    "TargetClosedError",
    "Target page, context or browser has been closed",
    "the handler is closed",
    "Event loop is closed",
)


class _PlaywrightTeardownFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.ERROR:
            return True
        text = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            text = f"{text} {record.exc_info[1]!r}"
        return not any(sig in text for sig in _TEARDOWN_SIGNATURES)


def setup_logging(settings: Settings) -> None:
    global _configured
    if _configured:
        return

    level = getattr(logging, (settings.log_level or "INFO").upper(), logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    log_path = Path(settings.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # These are chatty and add nothing here.
    for noisy in ("apscheduler.executors.default", "httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger("asyncio").addFilter(_PlaywrightTeardownFilter())

    _configured = True
