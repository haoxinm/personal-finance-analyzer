"""End-to-end privacy checks for tax imports (plan 3.4 / WP4): full SSNs/EINs never reach the
disk (outside the PDF a user explicitly keeps), the DB, our logs, or an LLM request."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from personal_finance_analyzer.classify import cascade
from personal_finance_analyzer.config import PrivacyConfig, Settings, save_settings
from personal_finance_analyzer.core.paths import documents_dir, extract_cache_dir, staging_dir
from personal_finance_analyzer.db.models import (
    Account,
    IncomeItem,
    Issuer,
    Statement,
    TaxDocument,
    TaxForm,
    TaxItem,
    Transaction,
    User,
)
from personal_finance_analyzer.db.session import make_session_factory
from personal_finance_analyzer.ingest.extract import extract
from personal_finance_analyzer.ingest.tax.pipeline import import_tax_document
from tests.ingest.tax_support import GENERATED_DIR
from tests.llm_fakes import FakeLLM, install, remote_cfg

_SSN_SHAPE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_EIN_SHAPE = re.compile(r"(?<!\d)\d{2}-\d{7}(?!\d)")
_NINE_DIGITS = re.compile(r"(?<!\d)\d{9}(?!\d)")
_ACCOUNT_SHAPE = re.compile(r"SYN-\d{6,}")


def _fixtures() -> list[Path]:
    return sorted(GENERATED_DIR.glob("tax_*/*.pdf"))


def _tin_needles(pdfs: list[Path]) -> set[str]:
    """Every full TIN / account number printed in any fixture, found by shape (and the digits-only
    spelling of each), so the test does not depend on the generator's constants."""
    needles: set[str] = set()
    for pdf in pdfs:
        text = extract(pdf, write_cache=False).full_text
        for pattern in (_SSN_SHAPE, _EIN_SHAPE, _ACCOUNT_SHAPE):
            for match in pattern.findall(text):
                needles.add(match)
                needles.add(re.sub(r"\D", "", match))
        needles.update(_NINE_DIGITS.findall(text))
    needles.discard("")
    return needles


@pytest.fixture
def privacy_home(home: Path, request: pytest.FixtureRequest) -> Path:
    keep_tax_pdfs: bool = getattr(request, "param", False)
    save_settings(
        Settings(
            privacy=PrivacyConfig(
                store_extract_cache=True,
                store_pdf_copies=True,
                store_tax_pdf_copies=keep_tax_pdfs,
            )
        )
    )
    return home


def _import_everything(session: Session) -> tuple[int, int]:
    user = User(name="alice")
    session.add(user)
    session.flush()
    parsed = 0
    for pdf in _fixtures():
        result = import_tax_document(
            session, pdf, user_id=user.id, original_name=pdf.name, tax_year=2024
        )
        parsed += result.status == "parsed"
    return user.id, parsed


@pytest.mark.parametrize("privacy_home", [False, True], indirect=True, ids=["no_pdf", "keep_pdf"])
def test_no_tin_on_disk_in_db_or_logs(
    privacy_home: Path,
    engine: Engine,
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    keep_tax_pdfs = load_keep_flag()
    pdfs = _fixtures()
    assert len(pdfs) > 20
    needles = _tin_needles(pdfs)
    assert len(needles) >= 6  # sanity: the fixtures do carry full identifiers
    caplog.set_level(logging.DEBUG)

    _, parsed = _import_everything(session)
    assert parsed > 10
    session.commit()
    engine.dispose()

    # (4) the debug extract cache holds no tax file even with store_extract_cache on
    cache = extract_cache_dir()
    assert not cache.exists() or list(cache.iterdir()) == []

    # (1) no file under the data home contains a full TIN; the kept-PDF directory is the only
    # allowed exception, and only when store_tax_pdf_copies is on.
    stmts = documents_dir()
    for path in privacy_home.rglob("*"):
        if not path.is_file():
            continue
        if stmts in path.parents:
            assert keep_tax_pdfs, f"tax PDF retained despite store_tax_pdf_copies=false: {path}"
            continue
        blob = path.read_bytes()
        for needle in needles:
            assert needle.encode() not in blob, f"identifier found in {path.name}"
    if not keep_tax_pdfs:
        assert not any(p.suffix == ".pdf" for p in stmts.rglob("*")) if stmts.exists() else True
    # an upload never lingers in staging after its import
    assert not staging_dir().exists() or list(staging_dir().glob("*.pdf")) == []

    # (2) the SQL dump has only last-4 fields
    con = sqlite3.connect(privacy_home / "finance.db")
    try:
        dump = "\n".join(con.iterdump())
    finally:
        con.close()
    assert re.search(r"INSERT INTO \"?tax_forms\"?", dump)
    for needle in needles:
        assert needle not in dump
    # sha256 hex (file hashes, fingerprints) can contain digit runs; they are not identifiers
    shapes_dump = re.sub(r"[0-9a-f]{64}", "", dump)
    for pattern in (_SSN_SHAPE, _EIN_SHAPE, _NINE_DIGITS):
        assert not pattern.search(shapes_dump)
    assert not _ACCOUNT_SHAPE.search(dump)

    # (3) our own logs (third-party pdfminer debug chatter is not ours) carry no identifier
    ours = "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith("personal_finance_analyzer")
    )
    assert "tax import" in ours
    for needle in needles:
        assert needle not in ours
    assert not _SSN_SHAPE.search(ours) and not _EIN_SHAPE.search(ours)


def load_keep_flag() -> bool:
    from personal_finance_analyzer.config import load_settings

    return load_settings().privacy.store_tax_pdf_copies


def _amount_spellings(minor: int) -> set[str]:
    dollars, cents = divmod(abs(minor), 100)
    return {str(abs(minor)), f"{dollars}.{cents:02d}", f"{dollars:,}.{cents:02d}"}


def test_import_and_classify_send_no_tax_content_to_the_llm(
    privacy_home: Path,
    engine: Engine,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PFA_LLM_API_KEY", "sk-test-key-not-real")
    fake = install(monkeypatch, FakeLLM(default=json.dumps({"items": []})))

    user_id, parsed = _import_everything(session)
    assert parsed > 10

    # A normal spending import in the same database, classified through the LLM cascade.
    issuer = Issuer(name=f"Issuer {uuid4().hex}", slug=f"issuer-{uuid4().hex}")
    session.add(issuer)
    session.flush()
    account = Account(user_id=user_id, issuer_id=issuer.id, account_type="credit", display_name="C")
    statement = Statement(file_sha256=uuid4().hex * 2, original_name="s.pdf", status="parsed")
    session.add_all([account, statement])
    session.flush()
    txn = Transaction(
        statement_id=statement.id,
        account_id=account.id,
        user_id=user_id,
        posted_date="2024-03-05",
        description_raw="ZXQ UNKNOWN VENDOR",
        description_clean="ZXQ UNKNOWN VENDOR",
        merchant_key="zxq unknown vendor",
        amount_minor=1234,
        currency="USD",
        kind="purchase",
        is_spend=True,
        dedupe_hash=uuid4().hex,
    )
    session.add(txn)
    session.commit()

    cascade.classify_transactions(
        make_session_factory(engine),
        [txn.id],
        cfg=remote_cfg(),
        group_id="tax-e2e",
        progress_cb=lambda *_: None,
    )
    preview = cascade.preview_egress(session, [txn.id])

    assert fake.requests, "the classify run must have made at least one (fake) LLM request"
    wire = json.dumps(fake.requests) + preview + json.dumps(fake.urls) + json.dumps(fake.headers)

    forbidden: set[str] = set()
    for payer in session.scalars(select(TaxForm.payer_name).distinct()):
        if payer:
            forbidden.add(payer)
    for name in session.scalars(select(TaxDocument.original_name)):
        forbidden.add(name)
    forbidden.update(_tin_needles(_fixtures()))
    for minor in session.scalars(select(IncomeItem.amount_minor)):
        if abs(minor) >= 100_000:
            forbidden.update(_amount_spellings(minor))
    for minor in session.scalars(select(TaxItem.amount_minor)):
        if minor >= 100_000:
            forbidden.update(_amount_spellings(minor))
    assert len(forbidden) > 20
    for item in forbidden:
        assert item not in wire, "tax content leaked into an LLM request"


# --- opt-in LLM fallback -------------------------------------------------------------------------

_UNREADABLE_PAGE = [
    "Form 1099-INT  Year-End Statement  Tax Year 2024",
    "Granite Fund Trust  summary of income paid to you during the year",
    "Payer identification  99-0000042      Account  SYN-000123456789",
    "RECIPIENT'S name: Jordan Q. Testperson",
    "RECIPIENT'S TIN: 912-34-5678",
    "Street address: 100 Example Lane",
    "Spouse reached at (312) 555-0100",
    "Interest paid to you                      1,234.56",
    "Please keep this statement with your tax records for the year.",
    "Questions about this statement may be sent to the customer service center.",
]


def _unreadable_pdf(tmp_path: Path) -> Path:
    from tests.fixtures.gen.base import render_lines_pdf

    path = tmp_path / "year_end.pdf"
    render_lines_pdf(path, [_UNREADABLE_PAGE])
    return path


def _enable_llm(*, fallback: bool) -> None:
    from personal_finance_analyzer.config import IngestConfig

    save_settings(
        Settings(
            llm=remote_cfg(),
            ingest=IngestConfig(tax_llm_fallback=fallback),
            privacy=PrivacyConfig(pii_terms=("Jordan Q. Testperson", "(312) 555-0100")),
        )
    )


def test_llm_fallback_off_by_default_even_with_llm_configured(
    home: Path, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from personal_finance_analyzer.api.services.tax_llm import build_tax_fallback

    monkeypatch.setenv("PFA_LLM_API_KEY", "sk-test-key-not-real")
    fake = install(monkeypatch, FakeLLM())  # a request would raise
    _enable_llm(fallback=False)
    user = User(name="alice")
    session.add(user)
    session.flush()
    assert build_tax_fallback(session, user.id) is None
    result = import_tax_document(
        session,
        _unreadable_pdf(tmp_path),
        user_id=user.id,
        original_name="y.pdf",
        fallback=build_tax_fallback(session, user.id),
    )
    assert result.status == "unsupported" and not result.llm_used
    assert fake.requests == []


def test_llm_fallback_request_is_redacted(
    home: Path, session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from personal_finance_analyzer.api.services.tax_llm import build_tax_fallback

    monkeypatch.setenv("PFA_LLM_API_KEY", "sk-test-key-not-real")
    reply = json.dumps(
        {
            "forms": [
                {
                    "form_type": "1099-int",
                    "payer_name": "Granite Fund Trust",
                    "payer_tin_last4": "0042",
                    "account_last4": "6789",
                    "pages": [1],
                    "items": [{"box": "1", "amount": "1,234.56"}],
                }
            ]
        }
    )
    fake = install(monkeypatch, FakeLLM(default=reply))
    _enable_llm(fallback=True)
    user = User(name="alice")
    session.add(user)
    session.flush()
    fallback = build_tax_fallback(session, user.id)
    assert fallback is not None
    result = import_tax_document(
        session,
        _unreadable_pdf(tmp_path),
        user_id=user.id,
        original_name="Jordan Testperson 912345678.pdf",
        fallback=fallback,
    )
    assert result.status == "parsed" and result.llm_used
    wire = json.dumps(fake.requests) + json.dumps(fake.urls) + json.dumps(fake.headers)
    for secret in (
        "912-34-5678",
        "912345678",
        "99-0000042",
        "990000042",
        "000123456789",
        "SYN-000123456789",
        "Jordan",
        "Testperson",
        "Example Lane",
        "555-0100",
        "y.pdf",
        "year_end.pdf",
        "Jordan Testperson 912345678.pdf",
    ):
        assert secret not in wire, secret
    for kept in ("5678", "0042", "6789", "1,234.56"):
        assert kept in wire
    assert {i.origin for i in session.scalars(select(IncomeItem))} == {"llm"}
    assert {f.extraction_method for f in session.scalars(select(TaxForm))} == {"llm"}
