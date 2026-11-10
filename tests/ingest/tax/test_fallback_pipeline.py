"""The opt-in LLM fallback inside `import_tax_document` (network-free: `FakeLLM`)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.config import IngestConfig, LLMConfig, Settings, save_settings
from personal_finance_analyzer.core.tax_types import (
    ParsedIncomeItem,
    ParsedTaxForm,
    ParsedTaxItem,
)
from personal_finance_analyzer.db.models import IncomeItem, TaxDocument, TaxForm, TaxItem, User
from personal_finance_analyzer.ingest.tax.fallback import _merge_forms
from personal_finance_analyzer.ingest.tax.pipeline import import_tax_document
from personal_finance_analyzer.llm.tax_parser import TaxLLMExtractor
from tests.fixtures.gen.base import render_lines_pdf
from tests.ingest.tax_support import generated_pdf
from tests.llm_fakes import FakeLLM, install, remote_cfg

_UNREADABLE = [
    "Form 1099-INT  Year-End Statement  Tax Year 2024",
    "Granite Fund Trust  summary of income paid to you during the year",
    "Payer identification  99-0000042      Account  SYN-000123456789",
    "Recipient  Jordan Q. Testperson    SSN 912-34-5678",
    "Mailing address  100 Example Lane Testville ZZ 00000",
    "Interest paid to you                      1,234.56",
    "Federal income tax withheld                  12.00",
    "Please keep this statement with your tax records for the year.",
    "Questions about this statement may be sent to the customer service center.",
]
_PARTIAL = [
    ["Tax Year 2024", "Form 1099-INT Interest Income", "1 Interest income   25.00"],
    [
        "Tax Year 2024",
        "Form 1099-R Distributions From Pensions",
        "1 Gross distribution   1,234.00",
    ],
]


def _user(session: Session) -> int:
    user = User(name="alice")
    session.add(user)
    session.flush()
    return user.id


def _pdf(tmp_path: Path, name: str, pages: list[list[str]]) -> Path:
    path = tmp_path / name
    render_lines_pdf(path, pages)
    return path


def _reply(**kw: Any) -> str:
    form: dict[str, Any] = {
        "form_type": "1099-int",
        "payer_name": "Granite Fund Trust",
        "payer_tin_last4": "0042",
        "account_last4": "6789",
        "pages": [1],
        "items": [{"box": "1", "amount": "1234.56"}, {"box": "4", "amount": "12.00"}],
    }
    form.update(kw)
    return json.dumps({"forms": [form]})


def _extractor() -> TaxLLMExtractor:
    return TaxLLMExtractor(remote_cfg(), ("Jordan Q. Testperson",))


def _import(session: Session, pdf: Path, uid: int, fallback: Any = None, **kw: Any) -> Any:
    return import_tax_document(
        session, pdf, user_id=uid, original_name=pdf.name, fallback=fallback, **kw
    )


def test_no_fallback_no_egress(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, FakeLLM())  # any request raises AssertionError
    result = _import(session, _pdf(tmp_path, "u.pdf", [_UNREADABLE]), _user(session))
    assert result.status == "unsupported" and not result.llm_used
    assert fake.requests == []


def test_whole_file_recovered_by_the_llm_and_tagged(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, FakeLLM(default=_reply()))
    uid = _user(session)
    result = _import(session, _pdf(tmp_path, "u.pdf", [_UNREADABLE]), uid, _extractor())
    assert result.status == "parsed", result.error
    assert (result.llm_used, result.llm_form_count, result.inserted_forms) == (True, 1, 1)
    assert any("review them" in w for w in result.warnings)
    form = session.scalars(select(TaxForm)).one()
    assert (form.extraction_method, form.tax_year, form.payer_tin_last4) == ("llm", 2024, "0042")
    assert {i.origin for i in session.scalars(select(IncomeItem))} == {"llm"}
    assert {t.origin for t in session.scalars(select(TaxItem))} == {"llm"}
    doc = session.scalars(select(TaxDocument)).one()
    assert doc.parser_id == "llm_tax"
    wire = json.dumps(fake.requests)
    for secret in ("912-34-5678", "99-0000042", "000123456789", "Testperson", "Example Lane"):
        assert secret not in wire


def test_no_write_transaction_is_open_during_the_llm_call(
    session: Session, tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = _user(session)
    session.commit()
    seen: list[str] = []

    def during_call(body: dict[str, Any]) -> str:
        con = sqlite3.connect(home / "finance.db", timeout=0)
        try:
            con.execute("BEGIN IMMEDIATE")  # fails with "database is locked" if a writer is open
            con.rollback()
            seen.append("writable")
        finally:
            con.close()
        assert not session.new and not session.dirty
        return _reply()

    install(monkeypatch, FakeLLM(default=during_call))
    result = _import(session, _pdf(tmp_path, "u.pdf", [_UNREADABLE]), uid, _extractor())
    assert result.status == "parsed" and seen == ["writable"]


def test_llm_error_keeps_the_deterministic_result(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, FakeLLM(default=(500, {"error": {"message": "boom"}})))
    result = _import(session, _pdf(tmp_path, "p.pdf", _PARTIAL), _user(session), _extractor())
    assert result.status == "parsed"
    assert result.form_types == ("1099-int",)
    assert any("LLM could not read" in w for w in result.warnings)
    assert {f.extraction_method for f in session.scalars(select(TaxForm))} == {"parser"}


def test_llm_error_on_a_whole_file_is_a_normal_failure_with_a_manual_hint(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, FakeLLM(default=(500, {"error": {"message": "boom"}})))
    result = _import(session, _pdf(tmp_path, "u.pdf", [_UNREADABLE]), _user(session), _extractor())
    assert result.status == "unsupported"
    assert result.error is not None and "add the figures manually" in result.error


def test_only_the_unparsed_region_is_sent_and_parser_forms_win(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = json.dumps(
        {
            "forms": [
                {
                    "form_type": "1099-misc",
                    "payer_name": "Pension Co",
                    "pages": [2],
                    "items": [{"box": "3", "amount": "1,234.00"}],
                }
            ]
        }
    )
    fake = install(monkeypatch, FakeLLM(default=reply))
    result = _import(session, _pdf(tmp_path, "p.pdf", _PARTIAL), _user(session), _extractor())
    assert result.form_types == ("1099-int", "1099-misc")
    assert len(fake.requests) == 1
    assert "=== PAGE 2" in fake.last_user_text and "=== PAGE 1" not in fake.last_user_text
    methods = {f.form_type: f.extraction_method for f in session.scalars(select(TaxForm))}
    assert methods == {"1099-int": "parser", "1099-misc": "llm"}


def test_parseable_file_never_calls_the_llm(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, FakeLLM())
    result = _import(session, generated_pdf("tax_w2", "normal"), _user(session), _extractor())
    assert result.status == "parsed" and not result.llm_used
    assert fake.requests == []


def test_llm_forms_go_through_the_same_strong_identity_dedupe(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, FakeLLM(default=_reply()))
    uid = _user(session)
    first = _import(session, _pdf(tmp_path, "a.pdf", [_UNREADABLE]), uid, _extractor())
    other = [[*_UNREADABLE, "Another page-one line so the file hash differs."]]
    second = _import(session, _pdf(tmp_path, "b.pdf", other), uid, _extractor())
    assert first.inserted_forms == 1
    assert (second.inserted_forms, second.skipped_duplicate_forms) == (0, 1)


def test_hint_to_turn_the_fallback_on_when_the_llm_is_configured_but_it_is_off(
    session: Session, tmp_path: Path
) -> None:
    save_settings(
        Settings(llm=LLMConfig(enabled=True, base_url="https://api.example.com", model="m"))
    )
    result = _import(session, _pdf(tmp_path, "u.pdf", [_UNREADABLE]), _user(session))
    assert result.status == "unsupported"
    assert result.error is not None
    assert "turn on the LLM fallback" in result.error and "manually" in result.error
    save_settings(Settings(ingest=IngestConfig(tax_llm_fallback=True)))


def _form(form_type: str, items: tuple[tuple[str, str], ...], **kw: Any) -> ParsedTaxForm:
    income = tuple(
        ParsedIncomeItem("interest", 100, src, box, None, origin=kw.get("origin", "parser"))
        for src, box in items
    )
    return ParsedTaxForm(
        form_type=form_type,  # type: ignore[arg-type]
        tax_year=2024,
        payer_name=None,
        payer_tin_last4=kw.get("tin"),
        account_last4=kw.get("acct"),
        is_corrected=False,
        filing_status=None,
        agi_minor=None,
        taxable_income_minor=None,
        income=income,
        taxes=(),
        extraction_method=kw.get("method", "parser"),
    )


def test_consolidated_llm_items_fill_only_sections_the_parser_did_not_read() -> None:
    det = _form("1099-consolidated", (("1099-DIV", "1a"),), acct="6789")
    llm = _form(
        "1099-consolidated",
        (("1099-DIV", "1a"), ("1099-INT", "1")),
        acct="6789",
        origin="llm",
        method="llm",
    )
    (merged,) = _merge_forms((det,), (llm,))
    assert merged.extraction_method == "parser"
    assert [(i.source_form, i.origin) for i in merged.income] == [
        ("1099-DIV", "parser"),
        ("1099-INT", "llm"),
    ]
    assert any("review them" in w for w in merged.warnings)
    assert isinstance(ParsedTaxItem, type)


# --- whole-file sends need evidence of a W-2 / 1099 form -----------------------------------

_BANK_STATEMENT = [
    [
        "Granite Savings Bank   Annual Account Summary 2024",
        "Account number  SYN-000123456789",
        "Interest earned this year                 1,234.56",
        "Fees charged this year                       12.00",
        "Your 1099-INT will be mailed separately if interest exceeds $10.",
    ],
    ["Page 2 of 2", "Month by month activity", "January interest   100.00"],
]


def test_bank_annual_statement_with_year_and_fallback_sends_nothing(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, FakeLLM(default=_reply()))
    result = _import(
        session,
        _pdf(tmp_path, "bank.pdf", _BANK_STATEMENT),
        _user(session),
        _extractor(),
        tax_year=2024,
    )
    assert fake.requests == [] and not result.llm_used
    assert result.status == "unsupported"


def test_only_pages_with_form_evidence_and_their_continuations_are_sent() -> None:
    from personal_finance_analyzer.ingest.tax.fallback import _whole_file_regions
    from tests.ingest.tax_support import doc_from_pages

    pages = [
        ["Cover letter from Granite Bank   total 999.00"],
        ["Form 1099-INT  Tax Year 2024", "PAYER'S name Granite Fund Trust", "Interest paid   5.00"],
        ["Details continued", "Fees   7.00"],
        ["Form 1040 U.S. Individual Income Tax Return 2024", "Wages 55,000.00"],
        ["Schedule notes", "Refund   123.00"],
        ["Form W-2 Wage and Tax Statement 2024", "1 Wages, tips, other compensation  9.00"],
    ]
    (region,) = _whole_file_regions(doc_from_pages(pages))
    assert region.pages == (2, 3, 6)  # not the cover, nor the 1040 and what follows it
    assert _whole_file_regions(doc_from_pages(_BANK_STATEMENT)) == ()


def test_a_1040_alone_is_never_sent(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = install(monkeypatch, FakeLLM(default=_reply()))
    pages = [["Form 1040 U.S. Individual Income Tax Return 2024", "Wages   55,000.00"]]
    _import(session, _pdf(tmp_path, "r.pdf", pages), _user(session), _extractor(), tax_year=2024)
    assert fake.requests == []


def test_requests_sent_is_reported_when_the_extractor_raises() -> None:
    from personal_finance_analyzer.core.tax_types import FallbackResult
    from personal_finance_analyzer.ingest.tax.fallback import apply_fallback
    from tests.ingest.tax_support import doc_from_pages

    class Boom:
        id = "boom"
        version = "1"
        requests_sent = 0

        def preview(self, doc: Any, regions: Any) -> list[str]:
            return []

        def extract(self, doc: Any, regions: Any, *, tax_year: int) -> FallbackResult:
            self.requests_sent = 2  # two requests went out, then something unexpected happened
            raise RuntimeError("unexpected")

    doc = doc_from_pages([["Form 1099-INT  Tax Year 2024", "Interest   5.00"]])
    run = apply_fallback(doc, None, Boom(), tax_year=2024)
    assert run.attempted and run.requests_sent == 2 and run.document is None


def test_text_only_pages_are_classified_as_form_1040() -> None:
    """Pages without word boxes still get the 1040 exclusion (and the pages after it)."""
    from dataclasses import replace

    from personal_finance_analyzer.ingest.tax.fallback import (
        _page_evidence,
        _whole_file_regions,
        other_form_pages,
    )
    from tests.ingest.tax_support import doc_from_pages

    pages = [
        ["Form 1040 U.S. Individual Income Tax Return 2024", "Wages 55,000.00"],
        ["Schedule notes", "Refund   123.00"],
        ["Form W-2 Wage and Tax Statement 2024", "1 Wages, tips, other compensation  9.00"],
    ]
    doc = doc_from_pages(pages)
    bare = replace(doc, pages=tuple(replace(p, words=()) for p in doc.pages))
    assert [_page_evidence(p) for p in bare.pages] == ["other", None, "form"]
    assert other_form_pages(bare) == frozenset({1, 2})
    (region,) = _whole_file_regions(bare)
    assert region.pages == (3,)
