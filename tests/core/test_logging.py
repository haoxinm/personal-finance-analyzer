from __future__ import annotations

import logging
from pathlib import Path

import pytest

from personal_finance_analyzer.core.logging import configure_logging, get_logger


def test_secret_shaped_string_is_redacted(home: Path) -> None:
    logger = configure_logging(home=home, console_level=logging.DEBUG)
    log_path = home / "logs" / "app.log"

    logger.warning("using api key %s", "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789")
    for handler in logger.handlers:
        handler.flush()

    content = log_path.read_text()
    assert "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789" not in content
    assert "[REDACTED]" in content


def test_configure_logging_is_idempotent(home: Path) -> None:
    logger1 = configure_logging(home=home)
    handler_count_1 = len(logger1.handlers)
    logger2 = configure_logging(home=home)
    assert logger1 is logger2
    assert len(logger2.handlers) == handler_count_1


def test_get_logger_returns_child_logger() -> None:
    child = get_logger("ingest")
    assert child.name == "personal_finance_analyzer.ingest"
    root = get_logger()
    assert root.name == "personal_finance_analyzer"


def test_pdf_libraries_never_log_document_text_at_debug(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """pdfminer logs extracted text at DEBUG; configure_logging must silence it (SSN safety)."""
    configure_logging(home=home, console_level=logging.DEBUG)
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        with caplog.at_level(logging.DEBUG):
            for name in ("pdfminer", "pdfminer.converter", "pdfplumber.page"):
                assert not logging.getLogger(name).isEnabledFor(logging.DEBUG)
                logging.getLogger(name).debug("text object 123-45-6789")
    finally:
        root.setLevel(previous)
    assert "123-45-6789" not in caplog.text
    # Warnings from these libraries still get through.
    assert logging.getLogger("pdfminer.converter").isEnabledFor(logging.WARNING)
