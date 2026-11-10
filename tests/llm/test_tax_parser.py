"""The tax LLM fallback (network-free): redaction, bounded requests, strict validation."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from personal_finance_analyzer.core.errors import LLMError
from personal_finance_analyzer.core.tax_types import UnparsedRegion
from personal_finance_analyzer.llm import tax_parser as tp
from tests.ingest.tax_support import doc_from_lines, doc_from_pages
from tests.llm_fakes import FakeLLM, install, remote_cfg

SSN = "912-34-5678"
EIN = "99-0000042"
ACCOUNT = "SYN-000123456789"
NAME = "Jordan Q. Testperson"
ADDRESS = "100 Example Lane"
PHONE = "(312) 555-0100"

_PAGE = [
    "Tax Year 2024",
    "Interest Summary",
    "PAYER'S name: Granite Fund Trust",
    f"PAYER'S TIN: {EIN}",
    f"RECIPIENT'S name: {NAME}",
    f"RECIPIENT'S TIN: {SSN}",
    f"Street address: {ADDRESS}",
    "City or town: Testville ZZ 00000",
    f"Account number: {ACCOUNT}",
    f"Phone on file {PHONE}",
    "Interest income   1,234.56",
    "Federal income tax withheld   12.00",
]
_REGION = (UnparsedRegion("unknown", (1,), "unreadable"),)


def _reply(*forms: dict[str, Any]) -> str:
    return json.dumps({"forms": list(forms)})


def _form(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "form_type": "1099-int",
        "payer_name": "Granite Fund Trust",
        "payer_tin_last4": "0042",
        "account_last4": "6789",
        "is_corrected": False,
        "pages": [1],
        "items": [
            {"box": "1", "amount": "1234.56"},
            {"box": "4", "amount": "12.00"},
        ],
    }
    base.update(kw)
    return base


def _extractor(**cfg: Any) -> tp.TaxLLMExtractor:
    return tp.TaxLLMExtractor(remote_cfg(**cfg), (NAME, ADDRESS, PHONE))


def _run(fake: FakeLLM, doc=None, regions=_REGION, **cfg: Any):  # type: ignore[no-untyped-def]
    return _extractor(**cfg).extract(doc or doc_from_lines(_PAGE), regions, tax_year=2024)


def test_valid_reply_gives_categories_from_the_box_map(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, FakeLLM(default=_reply(_form())))
    result = _run(fake)
    (form,) = result.forms
    assert (form.form_type, form.tax_year, form.extraction_method) == ("1099-int", 2024, "llm")
    assert form.payer_tin_last4 == "0042" and form.account_last4 == "6789"
    assert [(i.category, i.amount_minor, i.origin, i.box) for i in form.income] == [
        ("interest", 123456, "llm", "1")
    ]
    assert [(t.category, t.amount_minor, t.origin) for t in form.taxes] == [
        ("federal_income_tax_withheld", 1200, "llm")
    ]
    assert "Some values were read by your LLM; review them." in form.warnings
    assert result.requests_sent == 1


def test_request_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, FakeLLM(default=_reply()))
    _run(fake)
    wire = json.dumps(fake.requests)
    for secret in (
        SSN,
        "912345678",
        EIN,
        "990000042",
        ACCOUNT,
        "000123456789",
        NAME,
        "Testperson",
        ADDRESS,
        PHONE,
        "555-0100",
        "Testville",
    ):
        assert secret not in wire, secret
    for kept in ("5678", "0042", "6789", "1,234.56", "12.00", "Granite Fund Trust"):
        assert kept in wire, kept
    assert not re.search(r"\d{9}", wire)


def test_configured_terms_are_applied_like_statements(monkeypatch: pytest.MonkeyPatch) -> None:
    """Names, addresses and phones configured for statements are removed from tax text too, with
    the same matching rules (case, spacing, punctuation, Last-First order, phone formats)."""
    lines = [
        "Tax Year 2024",
        "Interest income   9.99",
        "Mailed to jordan  q.  TESTPERSON at 100 example lane",
        "Testperson, Jordan Q. called 312.555.0100",
    ]
    fake = install(monkeypatch, FakeLLM(default=_reply()))
    _extractor().extract(doc_from_lines(lines), _REGION, tax_year=2024)
    wire = json.dumps(fake.requests).lower()
    for secret in ("jordan", "testperson", "example lane", "555.0100", "312"):
        assert secret not in wire, secret
    assert "9.99" in wire


def test_unknown_box_negative_wage_and_ungrounded_amount_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    form = _form(
        items=[
            {"box": "1", "amount": "1,234.56"},
            {"box": "99", "amount": "12.00"},  # not on the allow-list
            {"box": "4", "amount": "-12.00"},  # negative tax
            {"box": "3", "amount": "777.77"},  # not printed anywhere
        ]
    )
    install(monkeypatch, FakeLLM(default=_reply(form)))
    result = _run(FakeLLM())
    (out,) = result.forms
    assert [i.amount_minor for i in out.income] == [123456]
    assert out.taxes == ()
    assert any("3 value(s)" in w and "rejected" in w for w in result.warnings)


def test_negative_gain_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = doc_from_lines(["Tax Year 2024", "Long-term (1,500.00)", "Short-term   20.00"])
    form = _form(
        form_type="1099-B",
        items=[{"box": "LT", "amount": "-1500.00"}, {"box": "ST", "amount": "20"}],
    )
    install(monkeypatch, FakeLLM(default=_reply(form)))
    result = _run(FakeLLM(), doc=doc)
    (out,) = result.forms
    assert {i.category: i.amount_minor for i in out.income} == {
        "long_term_capital_gain": -150000,
        "short_term_capital_gain": 2000,
    }


def test_1040_and_unknown_form_types_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeLLM(default=_reply(_form(form_type="1040"), _form(form_type="x"))))
    result = _run(FakeLLM())
    assert result.forms == ()
    assert any("2 form(s)" in w for w in result.warnings)


def test_identifiers_are_reduced_to_last_four(monkeypatch: pytest.MonkeyPatch) -> None:
    form = _form(payer_tin_last4="990000042", account_last4="12", payer_name=SSN)
    install(monkeypatch, FakeLLM(default=_reply(form)))
    (out,) = _run(FakeLLM()).forms
    assert out.payer_tin_last4 == "0042"
    assert out.account_last4 is None  # shorter than four digits
    assert out.payer_name is None  # SSN-shaped


def test_ungrounded_last4_and_placeholder_name_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    form = _form(payer_tin_last4="1111", payer_name="[NAME]")
    install(monkeypatch, FakeLLM(default=_reply(form)))
    (out,) = _run(FakeLLM()).forms
    assert (out.payer_tin_last4, out.payer_name) == (None, None)


def test_tax_year_is_never_taken_from_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    form = _form()
    form["tax_year"] = 1999
    install(monkeypatch, FakeLLM(default=_reply(form)))
    (out,) = _run(FakeLLM()).forms
    assert out.tax_year == 2024


def test_bad_json_gives_a_warning_not_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeLLM(default="I could not do that"))
    result = _run(FakeLLM())
    assert result.forms == ()
    assert result.warnings == (
        "The LLM could not read part of this file: the reply was not valid JSON.",
    )


def test_llm_error_gives_a_warning_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, FakeLLM(default=(500, {"error": {"message": "boom"}})))
    pages = [_PAGE, _PAGE, _PAGE]
    doc = doc_from_pages(pages)
    region = (UnparsedRegion("unknown", (1, 2, 3), "unreadable"),)
    result = _extractor().extract(doc, region, tax_year=2024)
    assert result.forms == ()
    assert result.warnings == ("The LLM could not read part of this file: the request failed.",)
    assert len(fake.requests) <= 2  # the 5xx retry of the first chunk only; later chunks skipped


def test_llm_error_type_is_llmerror() -> None:
    assert issubclass(LLMError, Exception)


def test_chunking_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [["Tax Year 2024", f"Interest income   {n}.25"] for n in range(1, 31)]
    doc = doc_from_pages(pages)
    region = (UnparsedRegion("unknown", tuple(range(1, 31)), "unreadable"),)
    requests, truncated = tp.build_requests(doc, region)
    assert len(requests) == tp.MAX_REQUESTS and truncated
    assert all(len(r.pages) <= tp.MAX_PAGES_PER_REQUEST for r in requests)
    with_payer = [["Tax Year 2024", "PAYER'S name: Granite Fund Trust", "Interest income   1.25"]]
    three, trunc = tp.build_requests(
        doc_from_pages([*with_payer, *pages[1:3]]), (UnparsedRegion("x", (1, 2, 3), "r"),)
    )
    assert [r.pages for r in three] == [(1, 2), (3,)] and not trunc
    assert "context only" in three[1].text and "Granite Fund Trust" in three[1].text
    bare, _ = tp.build_requests(doc_from_pages(pages[:3]), (UnparsedRegion("x", (1, 2, 3), "r"),))
    assert "context only" not in bare[1].text  # no payer label on page 1: no header
    fake = install(monkeypatch, FakeLLM(default=_reply()))
    result = _extractor().extract(doc, region, tax_year=2024)
    assert len(fake.requests) == tp.MAX_REQUESTS
    assert tp.TRUNCATED_WARNING in result.warnings


def test_only_pages_of_the_regions_are_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = doc_from_pages(
        [["Page one SECRETPAGE   1.00"], ["Tax Year 2024", "Interest income   2.00"]]
    )
    fake = install(monkeypatch, FakeLLM(default=_reply()))
    _extractor().extract(doc, (UnparsedRegion("1099-INT", (2,), "unrecognized"),), tax_year=2024)
    wire = json.dumps(fake.requests)
    assert "2.00" in wire and "=== PAGE 1" not in wire
    assert "PAGE 2" in wire and "PAGE 1 ===" not in wire


def test_no_request_without_any_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch, FakeLLM(default=_reply()))
    result = _extractor().extract(
        doc_from_lines(["Tax Year 2024", "Nothing here"]), _REGION, tax_year=2024
    )
    assert fake.requests == [] and result.requests_sent == 0


def test_redact_tax_text_without_word_boxes() -> None:
    text = f"RECIPIENT'S name\n{NAME}\n{ADDRESS}\nInterest 5.00\nSSN {SSN} EIN {EIN} acct {ACCOUNT}"
    out = tp.redact_tax_text(text, (NAME,))
    for secret in (NAME, ADDRESS, SSN, EIN, "000123456789"):
        assert secret not in out
    assert "5.00" in out and "5678" in out


def test_consolidated_keys_use_the_section_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    form = _form(
        form_type="1099-consolidated",
        items=[{"box": "INT:1", "amount": "1234.56"}, {"box": "DIV:1a", "amount": "12.00"}],
    )
    install(monkeypatch, FakeLLM(default=_reply(form)))
    (out,) = _run(FakeLLM()).forms
    assert {(i.source_form, i.box, i.category) for i in out.income} == {
        ("1099-INT", "1", "interest"),
        ("1099-DIV", "1a", "ordinary_dividends"),
    }


# --- last-four grounding -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value", "kept"),
    [
        ("Tax Year 2024\nInterest income 5.00", "2024", False),  # a year is not an identifier
        ("Interest income 2,024.00 on 1234 shares", "1234", False),
        ("Payer EIN **-***0042", "0042", True),
        ("acct ***1234", "1234", True),
        ("Account XXXX1234", "1234", True),
        ("SSN xxx-xx-1234", "1234", True),
        ("Account number: 1234", "1234", True),
        ("Payer ID 4455", "4455", True),
        ("TIN: 4455", "4455", True),
        ("Routing 4455", "4455", False),
    ],
)
def test_last4_must_be_masked_or_next_to_an_identifier_label(
    text: str, value: str, kept: bool
) -> None:
    assert (tp._last4(value, text) == value) is kept


# --- 2-up pages: only the unparsed form is sent and re-read forms are dropped ----------------

_RIGHT = 62


def _two(left: str, right: str) -> str:
    return left.ljust(_RIGHT) + right


_TWO_UP = [
    "Payment statement Tax Year 2024",
    _two("Form 1099-INT Interest Income", "Form 1099-INT Interest Income"),
    _two("PAYER'S name Alpha Bank", "PAYER'S name Beta Credit"),
    _two("Account number 000011116789", "Account number 000022224321"),
    _two("1 Interest income   100.00", "1 Interest income"),
    _two("4 Federal tax withheld   1.00", "4 Federal tax withheld   2.00"),
]


def test_two_up_page_only_the_failed_form_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    from personal_finance_analyzer.ingest.tax.fallback import apply_fallback
    from personal_finance_analyzer.ingest.tax.orchestrator import parse_document

    doc = doc_from_lines(_TWO_UP)
    outcome = parse_document(doc)
    assert outcome.document is not None and len(outcome.document.forms) == 1
    (region,) = outcome.document.unparsed
    assert region.pages == (1,) and region.boxes  # the rectangle of the right-hand form
    left = {"box": "1", "amount": "100.00"}
    right = {"box": "4", "amount": "2.00"}
    reply = _reply(
        _form(payer_name="Alpha Bank", account_last4="6789", items=[left]),  # re-read left form
        _form(payer_name="Beta Credit", account_last4="4321", payer_tin_last4=None, items=[right]),
    )
    fake = install(monkeypatch, FakeLLM(default=reply))
    run = apply_fallback(doc, outcome, _extractor(), tax_year=None)
    wire = json.dumps(fake.requests)
    assert "Beta Credit" in wire and "2.00" in wire
    for left_only in ("Alpha Bank", "100.00", "6789"):
        assert left_only not in wire, left_only
    assert run.document is not None
    # the parser's form stays, plus the one new LLM form: nothing is counted twice
    assert [f.extraction_method for f in run.document.forms] == ["parser", "llm"]
    totals = sorted(i.amount_minor for f in run.document.forms for i in f.income)
    assert totals == [10000]


def test_llm_form_that_repeats_a_parsed_form_is_dropped_by_amounts_and_by_account() -> None:
    from personal_finance_analyzer.core.tax_types import ParsedIncomeItem, ParsedTaxForm
    from personal_finance_analyzer.ingest.tax.fallback import _merge_forms

    def form(amounts: list[int], acct: str | None, tin: str | None, method: str) -> ParsedTaxForm:
        return ParsedTaxForm(
            form_type="1099-int",
            tax_year=2024,
            payer_name=None,
            payer_tin_last4=tin,
            account_last4=acct,
            is_corrected=False,
            filing_status=None,
            agi_minor=None,
            taxable_income_minor=None,
            income=tuple(
                ParsedIncomeItem("interest", a, "1099-INT", str(n), None)
                for n, a in enumerate(amounts, start=1)
            ),
            taxes=(),
            extraction_method=method,  # type: ignore[arg-type]
        )

    det = form([10000, 500], "6789", "0042", "parser")
    same_acct = form([99999], "6789", None, "llm")
    same_tin = form([77777], None, "0042", "llm")
    mostly_same = form([10000, 31337], None, None, "llm")  # one of two items matches
    other = form([20000], "4321", "9999", "llm")
    assert _merge_forms((det,), (same_acct, same_tin, mostly_same)) == (det,)
    assert _merge_forms((det,), (other,)) == (det, other)
    # two forms of one payer with different accounts are not duplicates of each other
    assert _merge_forms((det,), (form([77777], "4321", "0042", "llm"),))[1:] != ()
