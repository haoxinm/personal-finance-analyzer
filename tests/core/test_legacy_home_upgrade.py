"""Upgrading from "Spend Analyzer": the data directory is renamed, and everything that records or
looks up a kept PDF by absolute path keeps working afterwards (re-parse, reading, deleting).

The old install is built with the real import pipeline inside a pre-rename ``SpendAnalyzer``
directory (a ``spend.db`` whose rows record paths into it); `prepare_home` then moves it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from personal_finance_analyzer.api.routers.tax import _stored_pdf
from personal_finance_analyzer.config import PrivacyConfig, Settings, save_settings
from personal_finance_analyzer.core import paths
from personal_finance_analyzer.db.migrate import upgrade_head
from personal_finance_analyzer.db.models import Issuer, Statement, TaxDocument, User
from personal_finance_analyzer.db.session import make_engine_for_path, make_session_factory
from personal_finance_analyzer.ingest import pipeline
from personal_finance_analyzer.ingest.tax.pipeline import delete_tax_document, import_tax_document
from tests.fixtures.gen.base import format_row, render_lines_pdf
from tests.ingest.tax_support import generated_pdf


@dataclass
class Install:
    new: Path
    legacy: Path
    statement_id: int
    tax_document_id: int
    user_id: int


def _statement_pdf(path: Path) -> None:
    widths = (10, 32, 14)
    lines = [
        "Previous Balance: $0.00",
        "New Balance: $87.43",
        "Statement Period: 01/01/2026 to 01/31/2026",
        "",
        format_row(["Date", "Description", "Amount"], widths),
        format_row(["01/03/2026", "GROCERY MART", "45.10"], widths),
        format_row(["01/05/2026", "COFFEE SHOP", "12.34"], widths),
        format_row(["01/09/2026", "ONLINE BOOKSTORE", "29.99"], widths),
    ]
    render_lines_pdf(path, [lines])


@pytest.fixture
def legacy_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Install]:
    """A pre-rename default data directory holding a statement and a tax document, both with their
    PDF copies kept, written by the real pipelines while the directory still had its old name."""
    monkeypatch.delenv(paths.PFA_HOME_ENV, raising=False)
    monkeypatch.delenv(paths.LEGACY_HOME_ENV, raising=False)
    new = tmp_path / "support" / "PersonalFinanceAnalyzer"
    legacy = tmp_path / "support" / "SpendAnalyzer"
    legacy.mkdir(parents=True)
    monkeypatch.setattr(paths, "default_home", lambda platform=None: new)
    monkeypatch.setattr(paths, "legacy_default_home", lambda platform=None: legacy)
    paths._warned.clear()

    assert paths.get_home() == legacy  # the old install is what the app sees until it is moved
    paths.ensure_home()
    save_settings(Settings(privacy=PrivacyConfig(store_pdf_copies=True, store_tax_pdf_copies=True)))
    db_file = legacy / "spend.db"
    upgrade_head(f"sqlite:///{db_file}")
    engine = make_engine_for_path(db_file)
    try:
        with make_session_factory(engine)() as session:
            user = User(name="alex", is_default=True)
            issuer = Issuer(name="Old Bank", slug="old-bank")
            session.add_all([user, issuer])
            session.flush()
            source = tmp_path / "statement.pdf"
            _statement_pdf(source)
            proposal = pipeline.propose_import(
                session, source, user_id=user.id, original_name="jan.pdf"
            )
            pipeline.confirm_import(
                session,
                proposal.statement_id,
                issuer_id=issuer.id,
                parser_id=proposal.parser_id,
                layout_spec_id=proposal.layout_spec_id,
                remember=False,
            )
            w2 = generated_pdf("tax_w2", "normal")
            tax = import_tax_document(session, w2, user_id=user.id, original_name="w2.pdf")
            session.commit()
            statement = session.get(Statement, proposal.statement_id)
            document = session.get(TaxDocument, tax.document_id)
            assert statement is not None and document is not None
            assert str(legacy) in (statement.stored_path or "")
            assert str(legacy) in (document.stored_path or "")
            install = Install(new, legacy, statement.id, document.id, user.id)
    finally:
        engine.dispose()
    yield install


@pytest.fixture
def upgraded(legacy_install: Install) -> Iterator[tuple[Install, Session]]:
    """The install after `prepare_home` moved it, with a session on its database."""
    assert paths.prepare_home() == legacy_install.new
    assert not legacy_install.legacy.exists()
    engine: Engine = make_engine_for_path(paths.db_path())
    try:
        with make_session_factory(engine)() as session:
            yield legacy_install, session
    finally:
        engine.dispose()


def _make_paths_stale(install: Install) -> None:
    """Undo the prefix rewrite, as after a crash between the move and the rewrite (or a database
    restored from a machine with a different home): the rows name a directory that is gone."""
    con = sqlite3.connect(paths.db_path())
    try:
        for table in ("statements", "tax_documents"):
            con.execute(
                f"UPDATE {table} SET stored_path = replace(stored_path, ?, ?)",
                (str(install.new), str(install.legacy)),
            )
        con.commit()
    finally:
        con.close()


@pytest.mark.parametrize("stale", [False, True], ids=["rewritten", "stale-paths"])
def test_reparse_reads_the_pdf_in_the_moved_directory(
    upgraded: tuple[Install, Session], stale: bool
) -> None:
    install, session = upgraded
    statement = session.get(Statement, install.statement_id)
    assert statement is not None and statement.stored_path is not None
    if not stale:
        assert statement.stored_path.startswith(str(install.new))
    else:
        _make_paths_stale(install)
        session.expire_all()
        statement = session.get(Statement, install.statement_id)
        assert statement is not None and statement.stored_path is not None
        assert statement.stored_path.startswith(str(install.legacy))
        assert not Path(statement.stored_path).exists()

    result = pipeline.reparse(
        session, install.statement_id, parser_id="generic_table", layout_spec_id=None
    )
    session.commit()

    assert result.inserted == 3


@pytest.mark.parametrize("stale", [False, True], ids=["rewritten", "stale-paths"])
def test_kept_tax_pdf_is_found_in_the_moved_directory(
    upgraded: tuple[Install, Session], stale: bool
) -> None:
    install, session = upgraded
    if stale:
        _make_paths_stale(install)
        session.expire_all()
    document = session.get(TaxDocument, install.tax_document_id)
    assert document is not None

    found = _stored_pdf(document)

    assert found is not None
    assert found.parent == install.new / "statements"
    assert found.is_file()


@pytest.mark.parametrize("stale", [False, True], ids=["rewritten", "stale-paths"])
def test_deleting_a_tax_document_removes_the_real_pdf_not_a_stale_path(
    upgraded: tuple[Install, Session], stale: bool
) -> None:
    install, session = upgraded
    if stale:
        _make_paths_stale(install)
        session.expire_all()
    document = session.get(TaxDocument, install.tax_document_id)
    assert document is not None
    real = install.new / "statements" / f"{document.file_sha256}.pdf"
    assert real.is_file()

    assert delete_tax_document(session, install.tax_document_id) is True

    assert not real.exists()  # not orphaned in the new directory
    assert not install.legacy.exists()  # and nothing was written back under the old name


def test_a_tax_pdf_shared_by_another_document_survives_a_delete(
    upgraded: tuple[Install, Session],
) -> None:
    install, session = upgraded
    other = User(name="sam")
    session.add(other)
    session.flush()
    document = session.get(TaxDocument, install.tax_document_id)
    assert document is not None
    second = import_tax_document(
        session, generated_pdf("tax_w2", "normal"), user_id=other.id, original_name="w2.pdf"
    )
    session.commit()
    real = install.new / "statements" / f"{document.file_sha256}.pdf"
    assert real.is_file()

    assert delete_tax_document(session, install.tax_document_id) is True
    assert real.is_file()  # the second user's document still keeps this copy
    assert delete_tax_document(session, second.document_id) is True
    assert not real.exists()
