"""`pfa documents ...` and the startup carry-over of the old statements/ folder."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from personal_finance_analyzer.cli import app
from personal_finance_analyzer.core.paths import PFA_HOME_ENV, documents_dir
from personal_finance_analyzer.db.models import Statement, TaxDocument, User
from personal_finance_analyzer.db.session import make_engine_for_path, make_session_factory
from tests.backup.support import STATEMENT_REL, TAX_REL, USER_NAME, seed_home

runner = CliRunner()


def _rows(home: Path) -> dict[str, str | None]:
    engine = make_engine_for_path(home / "finance.db")
    try:
        with make_session_factory(engine)() as session:
            return {
                "statement": session.scalars(select(Statement.stored_path)).one(),
                "tax": session.scalars(select(TaxDocument.stored_path)).one(),
            }
    finally:
        engine.dispose()


def _rename_person(home: Path, name: str) -> None:
    engine = make_engine_for_path(home / "finance.db")
    try:
        with make_session_factory(engine)() as session:
            user = session.scalars(select(User)).one()
            user.name = name
            session.commit()
    finally:
        engine.dispose()


def test_where_prints_the_folder(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = runner.invoke(app, ["documents", "where"])
    assert result.exit_code == 0 and result.output.strip() == str(home / "documents")
    monkeypatch.setenv("PFA_DOCUMENTS_DIR", str(tmp_path / "nas"))
    assert runner.invoke(app, ["documents", "where"]).output.strip() == str(tmp_path / "nas")


def test_check_is_clean_for_a_consistent_folder(home: Path) -> None:
    seed_home(home)
    result = runner.invoke(app, ["documents", "check"])
    assert result.exit_code == 0, result.output
    assert "Everything the database refers to is in the folder" in result.output


def test_check_lists_missing_moved_and_unreferenced_and_exits_1_for_missing(home: Path) -> None:
    seed_home(home)
    docs = home / "documents"
    (docs / TAX_REL).unlink()
    (docs / STATEMENT_REL).rename(docs / "elsewhere.pdf")
    (docs / "stray.pdf").write_bytes(b"%PDF- stray")
    result = runner.invoke(app, ["documents", "check"])
    assert result.exit_code == 1
    assert "Missing (1)" in result.output and TAX_REL in result.output
    assert "Moved by hand (1)" in result.output and "now at elsewhere.pdf" in result.output
    assert "Not referenced" in result.output and "stray.pdf" in result.output


def test_check_changes_nothing(home: Path) -> None:
    seed_home(home)
    (home / "documents" / "stray.pdf").write_bytes(b"%PDF- stray")
    before = sorted(p.relative_to(home).as_posix() for p in home.rglob("*"))
    runner.invoke(app, ["documents", "check"])
    assert sorted(p.relative_to(home).as_posix() for p in home.rglob("*")) == before


def test_reorganize_dry_run_then_real(home: Path) -> None:
    seed_home(home)
    _rename_person(home, "Alexandra Q")

    dry = runner.invoke(app, ["documents", "reorganize", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "Would rename 2 file(s)" in dry.output and "Dry run: nothing was changed." in dry.output
    assert (
        "-> Alexandra Q/Statements/Fixture Bank/2026-01 Fixture Bank credit 9876.pdf" in dry.output
    )
    assert (home / "documents" / STATEMENT_REL).is_file()
    assert _rows(home)["statement"] == STATEMENT_REL

    real = runner.invoke(app, ["documents", "reorganize"])
    assert real.exit_code == 0, real.output
    assert "Renamed 2 file(s)" in real.output
    rows = _rows(home)
    assert (
        rows["statement"]
        == "Alexandra Q/Statements/Fixture Bank/2026-01 Fixture Bank credit 9876.pdf"
    )
    assert rows["tax"] == "Alexandra Q/Tax/2025/W-2 - Acme Corp.pdf"
    assert (home / "documents" / str(rows["statement"])).is_file()
    # The old person's emptied year/institution folders go; the person and drop folders stay.
    assert not (home / "documents" / USER_NAME / "Statements" / "Fixture Bank").exists()
    assert not any((home / "documents" / USER_NAME).rglob("*.pdf"))

    again = runner.invoke(app, ["documents", "reorganize"])
    assert "Nothing to rename" in again.output


def test_reorganize_reports_a_deleted_pdf(home: Path) -> None:
    seed_home(home)
    (home / "documents" / TAX_REL).unlink()
    result = runner.invoke(app, ["documents", "reorganize"])
    assert result.exit_code == 0
    assert "1 row(s) have no PDF in the folder any more" in result.output


# --- startup ------------------------------------------------------------------------------------


def test_migrate_carries_the_old_statements_folder_over(home: Path) -> None:
    shas = seed_home(home, legacy=True)
    assert (home / "statements").is_dir()
    result = runner.invoke(app, ["migrate"])
    assert result.exit_code == 0, result.output
    rows = _rows(home)
    assert rows["statement"] == STATEMENT_REL
    assert rows["tax"] == TAX_REL
    # Only the hash-named files moved; a stray partial upload is never touched (nor deleted).
    assert [p.name for p in (home / "statements").iterdir()] == ["tmpabcd1234.pdf"]
    for rel, sha in ((STATEMENT_REL, shas["statement_sha"]), (TAX_REL, shas["tax_sha"])):
        data = (home / "documents" / rel).read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha
    assert (home / "documents" / "README.txt").is_file()


def test_a_database_command_also_carries_them_over_and_a_second_run_is_a_no_op(
    home: Path,
) -> None:
    seed_home(home, legacy=True)
    first = runner.invoke(app, ["users", "list"])
    assert first.exit_code == 0, first.output
    assert [p.name for p in (home / "statements").iterdir()] == ["tmpabcd1234.pdf"]
    snapshot = {
        p.relative_to(home).as_posix(): p.stat().st_mtime_ns
        for p in (home / "documents").rglob("*")
    }
    runner.invoke(app, ["users", "list"])
    assert snapshot == {
        p.relative_to(home).as_posix(): p.stat().st_mtime_ns
        for p in (home / "documents").rglob("*")
    }


def test_a_missing_database_is_not_a_problem_for_check_on_an_empty_home(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PFA_HOME_ENV, str(home))
    assert runner.invoke(app, ["migrate"]).exit_code == 0
    result = runner.invoke(app, ["documents", "check"])
    assert result.exit_code == 0 and documents_dir().is_dir()
