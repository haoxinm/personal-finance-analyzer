"""Logging setup with mandatory secret redaction (§6.3, P0-5).

No `description_raw`, no amount tied to identity, and no secret may ever reach a log line. This
module cannot guarantee callers never pass such values, but it guarantees that anything shaped
like an API key is redacted before it reaches a handler, by installing a `logging.Filter` on every
handler this module attaches.

Log level is configurable; the console handler defaults to WARNING. A rotating file handler
always writes to ``<PFA_HOME>/logs/app.log`` at DEBUG, so a user can attach it to a bug
report without turning up console verbosity.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

#: Matches common API-key shapes: provider-prefixed secrets (`sk-...`, `sk-ant-...`) and long
#: base64/hex-ish runs that are unlikely to appear in legitimate log content.
_SECRET_RE = re.compile(
    r"(?i)\b(?:sk-ant-[a-z0-9\-_]{10,}|sk-[a-z0-9\-_]{10,}|[a-zA-Z0-9+/_\-]{32,})\b"
)

_REDACTED = "[REDACTED]"

_LOGGER_NAME = "personal_finance_analyzer"

#: Third-party PDF libraries log extracted document text at DEBUG (pdfminer echoes text objects).
#: Tax forms carry SSNs, so these are pinned to WARNING no matter how verbose our own logging is.
_QUIET_THIRD_PARTY_LOGGERS = ("pdfminer", "pdfplumber")


class _RedactSecretsFilter(logging.Filter):
    """Redacts any API-key-shaped substring from a log record's rendered message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - getMessage() itself failing is not our concern
            return True
        redacted = _SECRET_RE.sub(_REDACTED, message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(
    *, home: Path, console_level: int = logging.WARNING, file_level: int = logging.DEBUG
) -> logging.Logger:
    """Configure and return the ``personal_finance_analyzer`` logger.

    Idempotent: calling this more than once replaces existing handlers rather than stacking
    duplicates. Also pins the `pdfminer` and `pdfplumber` loggers to WARNING so document text
    (e.g. a tax form's SSN) can never be logged, even at DEBUG.

    Args:
        home: the data directory (`PFA_HOME`); logs are written to ``home/logs/app.log``.
        console_level: logging level for the console (stderr) handler.
        file_level: logging level for the rotating file handler.

    Returns:
        The configured logger.
    """
    for name in _QUIET_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    redact_filter = _RedactSecretsFilter()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(redact_filter)
    logger.addHandler(console_handler)

    logs_dir = home / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        logs_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redact_filter)
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger of ``personal_finance_analyzer`` (or the root app logger if ``name`` is None).

    Does not configure handlers; call `configure_logging` once at startup.
    """
    if name is None:
        return logging.getLogger(_LOGGER_NAME)
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")
