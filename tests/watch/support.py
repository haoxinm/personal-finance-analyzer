"""Helpers for the folder-import tests: people, a documents folder with files in it, a scanner."""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from personal_finance_analyzer.core.paths import documents_dir
from personal_finance_analyzer.db.models import Issuer, Statement, TaxDocument, User, WatchedFile
from personal_finance_analyzer.db.session import make_session_factory
from personal_finance_analyzer.watch.scanner import FolderScanner, ScanReport
from tests.fixtures.gen.base import format_row, render_lines_pdf
from tests.ingest.tax_support import generated_pdf

BANK = "Fixture Bank"
_WIDTHS = (10, 32, 14)


def add_person(session: Session, name: str, *, default: bool = False) -> User:
    user = User(name=name, is_default=default)
    session.add(user)
    session.commit()
    return user


def add_bank(session: Session, name: str = BANK) -> Issuer:
    issuer = Issuer(name=name, slug=name.lower().replace(" ", "-"))
    session.add(issuer)
    session.commit()
    return issuer


def statement_pdf(path: Path, *, bank: str = "Invented Card Co") -> Path:
    """A small card statement in a plain table: parseable, but never a *confident* proposal (no
    built-in layout matches it), so the app asks for an extractor."""
    names = ("GROCERY MART", "COFFEE SHOP", "ONLINE BOOKSTORE")
    lines = [
        bank,
        "Previous Balance: $0.00",
        "New Balance: $87.43",
        "Statement Period: 01/01/2026 to 01/31/2026",
        "",
        format_row(["Date", "Description", "Amount"], _WIDTHS),
        format_row(["01/03/2026", names[0], "45.10"], _WIDTHS),
        format_row(["01/05/2026", names[1], "12.34"], _WIDTHS),
        format_row(["01/09/2026", names[2], "29.99"], _WIDTHS),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    render_lines_pdf(path, [lines])
    return path


def put_statement(rel: str, variant: str = "normal") -> Path:
    """A statement the app recognises confidently (Layout A, issuer "Fixture Bank"); each
    ``variant`` is a different file."""
    dest = documents_dir() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(generated_pdf("layout_a_credit", variant), dest)
    return dest


def put_tax(rel: str, variant: str = "normal") -> Path:
    dest = documents_dir() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(generated_pdf("tax_w2", variant), dest)
    return dest


def put_pdf_bytes(rel: str, data: bytes) -> Path:
    dest = documents_dir() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def new_scanner(engine: Engine) -> FolderScanner:
    """A scanner that never really sleeps (a manual scan's settle pause is instant)."""
    return FolderScanner(make_session_factory(engine), sleep=lambda _s: None)


def scan_stable(scanner: FolderScanner) -> ScanReport:
    """Scan, treating files seen for the first time as already settled (like a manual scan)."""
    return scanner.scan(settle_seconds=1.0)


def actions(report: ScanReport) -> dict[str, str]:
    return {i.path: i.action for i in report.items}


def records(session: Session) -> dict[str, WatchedFile]:
    session.expire_all()
    return {r.path: r for r in session.scalars(select(WatchedFile))}


def statements(session: Session) -> list[Statement]:
    session.expire_all()
    return list(session.scalars(select(Statement).order_by(Statement.id)))


def tax_documents(session: Session) -> list[TaxDocument]:
    session.expire_all()
    return list(session.scalars(select(TaxDocument).order_by(TaxDocument.id)))
