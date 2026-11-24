"""Helpers for the folder-import tests: people, a documents folder with files in it, a scanner."""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from personal_finance_analyzer.core.paths import documents_dir
from personal_finance_analyzer.db.models import (
    Issuer,
    LayoutSpec,
    Statement,
    TaxDocument,
    User,
    WatchedFile,
)
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


_LAYOUT_B_SPEC = Path(__file__).resolve().parent.parent / "ingest" / "specs" / "layout_b.yaml"
LAYOUT_B_BANK = "Bank B"


def add_layout_b_bank(session: Session, name: str = LAYOUT_B_BANK) -> Issuer:
    """An issuer recognised on the Layout B fixture (`put_layout_b`)."""
    issuer = Issuer(
        name=name, slug=name.lower().replace(" ", "-"), match_terms='["ACCOUNT ACTIVITY"]'
    )
    session.add(issuer)
    session.commit()
    return issuer


def add_layout_b_spec(
    session: Session,
    issuer: Issuer,
    *,
    wrong: bool = False,
    approved: bool = True,
    name: str = "Spec B",
    score: float = 0.99,
) -> LayoutSpec:
    """A layout spec for the Layout B fixture. ``wrong`` moves the date column off the page, so it
    "parses" the fixture cleanly but finds no rows. Its detect score (default 0.99) beats the
    built-in Layout B parser, so it is what gets proposed."""
    text = _LAYOUT_B_SPEC.read_text().replace("score: 0.85", f"score: {score}")
    if wrong:
        text = text.replace("x0: 45, x1: 165", "x0: 600, x1: 700")
    slug = name.lower().replace(" ", "_")
    row = LayoutSpec(
        name=name,
        version=1,
        parser_id=f"spec_{slug}_v1",
        issuer_id=issuer.id,
        account_type="credit",
        spec_yaml=text,
        source="pasted",
        approved=approved,
    )
    session.add(row)
    session.commit()
    return row


def put_layout_b(rel: str, variant: str = "normal") -> Path:
    dest = documents_dir() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(generated_pdf("layout_b_credit", variant), dest)
    return dest
