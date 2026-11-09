"""`pfa documents scan [--dry-run]`: one-off folder imports without the server."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from personal_finance_analyzer.cli import app
from personal_finance_analyzer.core.filelock import FileLock
from personal_finance_analyzer.db.models import TaxDocument, WatchedFile
from personal_finance_analyzer.db.session import make_engine_for_path, make_session_factory
from personal_finance_analyzer.watch import scanner
from tests.backup.support import USER_NAME, seed_home
from tests.watch.support import put_statement, put_tax

runner = CliRunner()

NEW_TAX = f"{USER_NAME}/Tax/2024/W-2 - Pinecrest.pdf"


@pytest.fixture(autouse=True)
def _quick_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scanner, "MANUAL_SETTLE_S", 0.01)


def _rows(home: Path) -> tuple[list[str | None], list[tuple[str, str]]]:
    engine = make_engine_for_path(home / "finance.db")
    try:
        with make_session_factory(engine)() as session:
            docs = list(session.scalars(select(TaxDocument.stored_path).order_by(TaxDocument.id)))
            watched = [(w.path, w.outcome) for w in session.scalars(select(WatchedFile))]
            return docs, watched
    finally:
        engine.dispose()


def test_dry_run_lists_what_would_be_imported_and_changes_nothing(home: Path) -> None:
    seed_home(home)
    put_tax(NEW_TAX)
    put_statement("Nobody/Statements/jan.pdf")
    before_docs, before_watched = _rows(home)
    result = runner.invoke(app, ["documents", "scan", "--dry-run"])
    assert result.exit_code == 1, result.output  # the unknown person needs attention
    assert f"would import: {NEW_TAX}" in result.output
    assert "NEEDS ATTENTION: Nobody/Statements/jan.pdf" in result.output
    assert 'No person is named "Nobody"' in result.output
    assert "Dry run: nothing was imported or recorded." in result.output
    assert _rows(home) == (before_docs, before_watched) == (before_docs, [])


def test_scan_imports_files_where_they_are_and_a_second_run_finds_nothing_new(
    home: Path,
) -> None:
    seed_home(home)
    put_tax(NEW_TAX)
    first = runner.invoke(app, ["documents", "scan"])
    assert first.exit_code == 0, first.output
    assert f"imported: {NEW_TAX}" in first.output and "1 imported" in first.output
    docs, watched = _rows(home)
    assert NEW_TAX in docs and (NEW_TAX, "imported") in watched
    assert (home / "documents" / NEW_TAX).is_file()
    second = runner.invoke(app, ["documents", "scan"])
    assert second.exit_code == 0 and "0 imported" in second.output
    assert "imported:" not in second.output.replace("0 imported", "")


def test_scan_exits_1_for_a_problem_and_remembers_it(home: Path) -> None:
    seed_home(home)
    put_statement("Nobody/Statements/jan.pdf")
    result = runner.invoke(app, ["documents", "scan"])
    assert result.exit_code == 1
    assert "NEEDS ATTENTION" in result.output and "1 need attention" in result.output
    assert ("Nobody/Statements/jan.pdf", "failed") in _rows(home)[1]


def test_scan_waits_its_turn_for_the_lock_and_says_so(home: Path) -> None:
    seed_home(home)
    put_tax(NEW_TAX)
    holder = FileLock(scanner.lock_path())
    assert holder.try_acquire()
    try:
        result = runner.invoke(app, ["documents", "scan"])
    finally:
        holder.release()
    assert result.exit_code == 3
    assert "Another scan is running" in result.output
    assert NEW_TAX not in _rows(home)[0]


def test_scan_refuses_an_old_database(home: Path) -> None:
    (home / "documents").mkdir(parents=True)
    result = runner.invoke(app, ["documents", "scan", "--dry-run"])
    assert result.exit_code == 2 and "pfa migrate" in result.output
