"""Tests for the layout spec format and interpreter (P1-H).

Built entirely from in-memory `ExtractedDoc`s (words with coordinates, no PDF, no reportlab) —
this WP needs nothing from the parallel parser lanes (P1-B/P1-B1..4).
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.types import ExtractedDoc, PageText, Word
from personal_finance_analyzer.db.models import Issuer, LayoutSpec
from personal_finance_analyzer.ingest.layout_spec import (
    _SCHEMA,
    LayoutSpecError,
    approve_spec,
    build_parser_for_row,
    load_spec,
    resolve_spec,
    revise,
    validate_against_schema,
)

# --------------------------------------------------------------------------------------------
# In-memory ExtractedDoc builders
# --------------------------------------------------------------------------------------------

_ROW_HEIGHT = 10.0


def _word(text: str, x0: float, x1: float, top: float) -> Word:
    return Word(text=text, x0=x0, x1=x1, top=top, bottom=top + _ROW_HEIGHT)


def _heading(text: str, top: float) -> list[Word]:
    """A single word spanning the row, for a section-heading/terminator/excluded-table line."""
    return [_word(text, 0.0, 400.0, top)]


def _row(top: float, **cells: str) -> list[Word]:
    """One data row. ``cells`` maps a known column name to its printed text; unset cells are
    omitted (blank)."""
    bands = {
        "posted_date": (0.0, 50.0),
        "description": (50.0, 200.0),
        "amount": (200.0, 260.0),
        "debit": (200.0, 260.0),
        "credit": (260.0, 320.0),
        "balance": (320.0, 380.0),
        "issuer_category": (380.0, 440.0),
    }
    words: list[Word] = []
    for name, text in cells.items():
        if not text:
            continue
        x0, x1 = bands[name]
        words.append(_word(text, x0 + 1.0, x1 - 1.0, top))
    return words


def _doc(page_text: str, page_words: list[Word]) -> ExtractedDoc:
    page = PageText(
        page_number=1, text=page_text, words=tuple(page_words), char_count=len(page_text)
    )
    return ExtractedDoc(
        file_sha256="deadbeef" * 8, page_count=1, pages=(page,), has_text_layer=True
    )


_PERIOD_TEXT = "ACCOUNT ACTIVITY\nStatement Date: 01/01/2024 Closing Date: 01/31/2024\nPURCHASES\n"


# --------------------------------------------------------------------------------------------
# Schema validation
# --------------------------------------------------------------------------------------------


def test_validate_against_schema_accepts_a_minimal_valid_doc() -> None:
    data = {
        "id": "acme",
        "version": 1,
        "account_type": "credit",
        "detect": {"score": 0.5},
        "columns": [
            {"name": "posted_date", "x0": 0, "x1": 10, "type": "date", "formats": ["%m/%d"]},
            {"name": "description", "x0": 10, "x1": 20, "type": "text"},
            {"name": "amount", "x0": 20, "x1": 30, "type": "money"},
        ],
    }
    assert validate_against_schema(data, _SCHEMA) == []


def test_validate_against_schema_reports_field_paths_for_multiple_errors() -> None:
    data = {
        "id": "acme",
        "version": "not-an-int",  # wrong type
        "account_type": "bitcoin",  # not in enum
        "detect": {"score": 0.5},
        "columns": [
            {"name": "posted_date", "x0": 0, "x1": 10, "type": "date", "formats": ["%m/%d"]},
        ],
        "unexpected_top_level_key": True,
    }
    errors = validate_against_schema(data, _SCHEMA)
    fields = {e.field for e in errors}
    assert "version" in fields
    assert "account_type" in fields
    assert "unexpected_top_level_key" in fields


# --------------------------------------------------------------------------------------------
# load_spec: rejection, field errors, no partial application
# --------------------------------------------------------------------------------------------


def test_load_spec_rejects_invalid_yaml() -> None:
    with pytest.raises(LayoutSpecError):
        load_spec("id: [this is not: valid yaml", source="pasted")


def test_load_spec_rejects_missing_required_column_with_field_path() -> None:
    text = """
id: bad_spec
version: 1
account_type: credit
detect: {score: 0.5}
columns:
  - {name: description, x0: 0, x1: 100, type: text}
  - {name: amount, x0: 100, x1: 200, type: money}
"""
    with pytest.raises(LayoutSpecError) as exc_info:
        load_spec(text, source="pasted")
    messages = [str(e) for e in exc_info.value.errors]
    assert any("posted_date" in m for m in messages)


def test_load_spec_rejects_column_with_x0_gte_x1() -> None:
    text = """
id: bad_spec
version: 1
account_type: credit
detect: {score: 0.5}
columns:
  - {name: posted_date, x0: 100, x1: 50, type: date, formats: ["%m/%d"]}
  - {name: description, x0: 100, x1: 200, type: text}
  - {name: amount, x0: 200, x1: 300, type: money}
"""
    with pytest.raises(LayoutSpecError) as exc_info:
        load_spec(text, source="user_authored")
    assert any(e.field == "columns[0]" for e in exc_info.value.errors)


def test_load_spec_rejects_pathological_regex_at_load_not_at_parse() -> None:
    """A nested-quantifier regex is rejected by `load_spec` itself; `parse()` is never reached."""
    text = """
id: evil_spec
version: 1
account_type: credit
detect: {score: 0.5}
columns:
  - {name: posted_date, x0: 0, x1: 50, type: date, formats: ["%m/%d"]}
  - {name: description, x0: 50, x1: 200, type: text}
  - {name: amount, x0: 200, x1: 260, type: money}
sections:
  mode: heading
  patterns:
    - {match: "(a+)+", kind_hint: purchase}
"""
    with pytest.raises(LayoutSpecError) as exc_info:
        load_spec(text, source="pasted")
    assert "backtrack" in str(exc_info.value)


def test_load_spec_treats_every_origin_identically_and_lands_unapproved() -> None:
    text = """
id: acme_credit
version: 1
account_type: credit
detect: {score: 0.5}
columns:
  - {name: posted_date, x0: 0, x1: 50, type: date, formats: ["%m/%d"]}
  - {name: description, x0: 50, x1: 200, type: text}
  - {name: amount, x0: 200, x1: 260, type: money}
"""
    pasted = load_spec(text, source="pasted")
    llm = load_spec(text, source="llm_proposed")
    authored = load_spec(text, source="user_authored")
    assert pasted.approved is False
    assert llm.approved is False
    assert authored.approved is False
    assert pasted.parser_id == llm.parser_id == authored.parser_id


# --------------------------------------------------------------------------------------------
# Interpreter: sections, exclude_tables, sign conventions, terminator (Layout-B-shaped)
# --------------------------------------------------------------------------------------------

_LAYOUT_B_SHAPED_SPEC = r"""
id: test_layout_b
version: 1
account_type: credit
detect:
  all_of: ["ACCOUNT ACTIVITY"]
  any_of: ["PURCHASES"]
  score: 0.9
columns:
  - {name: posted_date, x0: 0, x1: 50, type: date, formats: ["%m/%d"]}
  - {name: description, x0: 50, x1: 200, type: text, multiline: true}
  - {name: amount, x0: 200, x1: 260, type: money}
sections:
  mode: heading
  patterns:
    - {match: "PURCHASES", kind_hint: purchase}
    - {match: "PAYMENTS AND OTHER CREDITS", kind_hint: payment_or_refund}
  terminator: "^TOTAL .* FOR THIS PERIOD$"
  exclude_tables: ["INTEREST CHARGED"]
sign: {outflow: unsigned, inflow: leading_minus}
year_inference: from_period
totals: {section_totals: true}
fx:
  pattern: '^FOREIGN CURRENCY AMOUNT\s+([\d,]+\.\d{2})\s+([A-Z]{3})\s+EXCH(?:ANGE)? RATE\s+([\d.]+)$'
  amount_group: 1
  currency_group: 2
  rate_group: 3
"""


def _layout_b_shaped_doc() -> ExtractedDoc:
    words: list[Word] = []
    top = 100.0

    def add(row_words: list[Word]) -> None:
        nonlocal top
        words.extend(row_words)
        top += _ROW_HEIGHT + 2.0

    add(_heading("PURCHASES", top))
    add(_row(top, posted_date="01/05", description="Coffee Shop", amount="4.50"))
    add(_row(top, posted_date="01/06", description="Grocery", amount="32.10"))
    add(_heading("PAYMENTS AND OTHER CREDITS", top))
    add(_row(top, posted_date="01/15", description="Payment Thank You", amount="-100.00"))
    # INTEREST CHARGED is the real Layout B ordering: it comes last, right before the
    # terminator, and once inside it a stray section-shaped row must not re-open a section.
    add(_heading("INTEREST CHARGED", top))
    add(_row(top, posted_date="01/07", description="Interest Charge", amount="1.99"))
    add(_heading("TOTAL NEW BALANCE FOR THIS PERIOD", top))
    add(_row(top, posted_date="01/20", description="Should Never Appear", amount="9.99"))
    return _doc(_PERIOD_TEXT, words)


def test_spec_parser_detect_score() -> None:
    loaded = load_spec(_LAYOUT_B_SHAPED_SPEC, source="user_authored")
    doc = _layout_b_shaped_doc()
    assert loaded.parser.detect(doc) == pytest.approx(0.9)

    # any_of term absent -> 0.0, never raises
    no_purchases_doc = _doc("ACCOUNT ACTIVITY only, nothing else", [])
    assert loaded.parser.detect(no_purchases_doc) == 0.0


def test_spec_parser_excludes_interest_table_and_applies_sign_conventions() -> None:
    loaded = load_spec(_LAYOUT_B_SHAPED_SPEC, source="user_authored")
    parsed = loaded.parser.parse(_layout_b_shaped_doc())

    # INTEREST CHARGED's one row is suppressed; the terminator stops parsing before the last row.
    assert len(parsed.transactions) == 3
    descriptions = [t.description for t in parsed.transactions]
    assert "Interest Charge" not in descriptions
    assert "Should Never Appear" not in descriptions

    coffee, grocery, payment = parsed.transactions
    assert coffee.amount_minor == 450  # outflow, unsigned as printed -> positive (I5)
    assert grocery.amount_minor == 3210
    assert payment.amount_minor == -10000  # inflow, leading_minus as printed -> negative (I5)
    assert payment.kind_hint == "payment_or_refund"
    assert coffee.kind_hint == "purchase"
    assert coffee.posted_date == date(2024, 1, 5)

    assert parsed.section_totals == (("PURCHASES", 3660), ("PAYMENTS AND OTHER CREDITS", -10000))
    assert parsed.period_start == date(2024, 1, 1)
    assert parsed.period_end == date(2024, 1, 31)


def test_excluded_table_state_survives_a_label_shaped_row_inside_it() -> None:
    """§2c: disambiguate by which table we are inside, never by label alone. A row inside
    INTEREST CHARGED that happens to start with a section label (the APR/finance-charge line,
    e.g. ``PURCHASES 22.99% 201.95 3.87``) must not re-open that section, and the interest rows
    that follow it must not become continuation text for the previous transaction."""
    words: list[Word] = []
    top = 100.0

    def add(row_words: list[Word]) -> None:
        nonlocal top
        words.extend(row_words)
        top += _ROW_HEIGHT + 2.0

    add(_heading("PURCHASES", top))
    add(_row(top, posted_date="01/05", description="Coffee Shop", amount="4.50"))
    add(_heading("INTEREST CHARGED", top))
    add(_heading("PURCHASES 22.99% 201.95 3.87", top))  # label-shaped APR row, not a real heading
    add(_row(top, posted_date="01/08", description="Interest Charge A", amount="1.99"))
    add(_heading("PURCHASES 24.99% 50.00 1.04", top))  # a second one, for good measure
    add(_row(top, posted_date="01/09", description="Interest Charge B", amount="0.87"))
    add(_heading("TOTAL NEW BALANCE FOR THIS PERIOD", top))
    doc = _doc(_PERIOD_TEXT, words)

    loaded = load_spec(_LAYOUT_B_SHAPED_SPEC, source="user_authored")
    parsed = loaded.parser.parse(doc)

    assert len(parsed.transactions) == 1
    coffee = parsed.transactions[0]
    assert coffee.description == "Coffee Shop"  # never merged with any interest-table text
    assert coffee.amount_minor == 450
    assert coffee.section == "PURCHASES"


def test_spec_parser_multiline_description_continuation() -> None:
    words: list[Word] = []
    top = 100.0

    def add(row_words: list[Word]) -> None:
        nonlocal top
        words.extend(row_words)
        top += _ROW_HEIGHT + 2.0

    add(_heading("PURCHASES", top))
    add(_row(top, posted_date="01/05", description="Some Merchant", amount="10.00"))
    add(_row(top, description="Ref# 12345 extra detail"))  # continuation: no date, no amount
    doc = _doc(_PERIOD_TEXT, words)

    loaded = load_spec(_LAYOUT_B_SHAPED_SPEC, source="user_authored")
    parsed = loaded.parser.parse(doc)

    assert len(parsed.transactions) == 1
    assert parsed.transactions[0].description == "Some Merchant Ref# 12345 extra detail"


def test_spec_parser_multiline_continuation_keeps_words_that_fall_in_other_bands() -> None:
    """A continuation line printed flush with the left margin lands in the `posted_date` band,
    not the `description` band — the whole row (words ordered by x0) must still be kept."""
    words: list[Word] = []
    top = 100.0

    def add(row_words: list[Word]) -> None:
        nonlocal top
        words.extend(row_words)
        top += _ROW_HEIGHT + 2.0

    add(_heading("PURCHASES", top))
    add(_row(top, posted_date="01/05", description="Some Merchant", amount="10.00"))
    # A continuation word placed inside the posted_date band (x0=0-50), not the description band.
    words.append(_word("Ref# 12345", 5.0, 49.0, top))
    top += _ROW_HEIGHT + 2.0
    doc = _doc(_PERIOD_TEXT, words)

    loaded = load_spec(_LAYOUT_B_SHAPED_SPEC, source="user_authored")
    parsed = loaded.parser.parse(doc)

    assert len(parsed.transactions) == 1
    assert parsed.transactions[0].description == "Some Merchant Ref# 12345"


def test_spec_parser_raises_parser_error_when_year_cannot_be_resolved() -> None:
    """No period found and dates carry no explicit year -> `ParserError`, not a silent guess."""
    words: list[Word] = []
    top = 100.0

    def add(row_words: list[Word]) -> None:
        nonlocal top
        words.extend(row_words)
        top += _ROW_HEIGHT + 2.0

    add(_heading("PURCHASES", top))
    add(_row(top, posted_date="01/05", description="Coffee Shop", amount="4.50"))
    doc = _doc("ACCOUNT ACTIVITY\nno period text here\n", words)

    loaded = load_spec(_LAYOUT_B_SHAPED_SPEC, source="user_authored")
    with pytest.raises(ParserError):
        loaded.parser.parse(doc)


# --------------------------------------------------------------------------------------------
# Interpreter: debit/credit column pair with a balance column (Layout-D-shaped, A26)
# --------------------------------------------------------------------------------------------

_BANK_SPEC = """
id: test_layout_d
version: 1
account_type: checking
detect: {all_of: ["ACCOUNT ACTIVITY"], score: 0.8}
columns:
  - {name: posted_date, x0: 0, x1: 50, type: date, formats: ["%m/%d"]}
  - {name: description, x0: 50, x1: 200, type: text}
  - {name: debit, x0: 200, x1: 260, type: money, role: debit}
  - {name: credit, x0: 260, x1: 320, type: money, role: credit}
  - {name: balance, x0: 320, x1: 380, type: balance}
year_inference: from_period
"""


def test_spec_parser_debit_credit_pair_with_balance_column_is_expressible() -> None:
    words: list[Word] = []
    top = 100.0

    def add(row_words: list[Word]) -> None:
        nonlocal top
        words.extend(row_words)
        top += _ROW_HEIGHT + 2.0

    add(
        _row(
            top, posted_date="01/05", description="Grocery Store", debit="45.67", balance="1000.00"
        )
    )
    add(
        _row(
            top,
            posted_date="01/10",
            description="Payroll Deposit",
            credit="2500.00",
            balance="3454.33",
        )
    )
    doc = _doc(_PERIOD_TEXT, words)

    loaded = load_spec(_BANK_SPEC, source="pasted")
    parsed = loaded.parser.parse(doc)

    assert len(parsed.transactions) == 2
    grocery, payroll = parsed.transactions
    assert grocery.amount_minor == 4567  # debit -> outflow, positive (I5)
    assert payroll.amount_minor == -250000  # credit -> inflow, negative (I5)


def test_load_spec_rejects_mixing_amount_column_with_debit_credit() -> None:
    text = """
id: bad_mix
version: 1
account_type: checking
detect: {score: 0.5}
columns:
  - {name: posted_date, x0: 0, x1: 50, type: date, formats: ["%m/%d"]}
  - {name: description, x0: 50, x1: 200, type: text}
  - {name: amount, x0: 200, x1: 260, type: money}
  - {name: debit, x0: 260, x1: 320, type: money, role: debit}
"""
    with pytest.raises(LayoutSpecError) as exc_info:
        load_spec(text, source="pasted")
    assert any(e.field == "columns" for e in exc_info.value.errors)


# --------------------------------------------------------------------------------------------
# account_mask: local-only, extracted from the document's text
# --------------------------------------------------------------------------------------------


def test_load_spec_rejects_account_mask_pattern_that_is_pathological() -> None:
    text = """
id: evil_mask
version: 1
account_type: credit
detect: {score: 0.5}
account_mask: {pattern: "(a+)+"}
columns:
  - {name: posted_date, x0: 0, x1: 50, type: date, formats: ["%m/%d"]}
  - {name: description, x0: 50, x1: 200, type: text}
  - {name: amount, x0: 200, x1: 260, type: money}
"""
    with pytest.raises(LayoutSpecError) as exc_info:
        load_spec(text, source="pasted")
    assert "backtrack" in str(exc_info.value)


def test_spec_parser_extracts_account_mask_last_four_digits() -> None:
    text = """
id: masked_spec
version: 1
account_type: credit
detect: {score: 0.5}
account_mask: {pattern: "ending in\\\\s+(\\\\d{4,6})"}
columns:
  - {name: posted_date, x0: 0, x1: 50, type: date, formats: ["%m/%d"]}
  - {name: description, x0: 50, x1: 200, type: text}
  - {name: amount, x0: 200, x1: 260, type: money}
"""
    loaded = load_spec(text, source="pasted")
    doc = _doc(
        _PERIOD_TEXT + "Account ending in 445678\n",
        _row(100.0, posted_date="01/05", description="X", amount="1.00"),
    )
    parsed = loaded.parser.parse(doc)
    assert parsed.account_hint.mask == "5678"  # last 4 only (I1b): local-only, never egressed


def test_spec_parser_account_mask_is_none_when_not_present() -> None:
    loaded = load_spec(_LAYOUT_B_SHAPED_SPEC, source="user_authored")
    parsed = loaded.parser.parse(_layout_b_shaped_doc())
    assert parsed.account_hint.mask is None


# --------------------------------------------------------------------------------------------
# fx: a dateless foreign-currency continuation line (§2c)
# --------------------------------------------------------------------------------------------


def test_spec_parser_fx_line_sets_previous_transaction_and_never_touches_description() -> None:
    words: list[Word] = []
    top = 100.0

    def add(row_words: list[Word]) -> None:
        nonlocal top
        words.extend(row_words)
        top += _ROW_HEIGHT + 2.0

    add(_heading("PURCHASES", top))
    add(_row(top, posted_date="01/05", description="Coffee Shop", amount="10.00"))
    add(_heading("FOREIGN CURRENCY AMOUNT 35.00 GBP EXCH RATE 1.20000", top))
    add(_row(top, posted_date="01/06", description="Grocery", amount="32.10"))  # untouched
    doc = _doc(_PERIOD_TEXT, words)

    loaded = load_spec(_LAYOUT_B_SHAPED_SPEC, source="user_authored")
    parsed = loaded.parser.parse(doc)

    assert len(parsed.transactions) == 2
    coffee, grocery = parsed.transactions
    assert coffee.description == "Coffee Shop"  # not "Coffee Shop FOREIGN CURRENCY AMOUNT ..."
    assert coffee.fx_amount_minor == 3500
    assert coffee.fx_currency == "GBP"
    assert coffee.fx_rate == pytest.approx(1.2)
    assert grocery.fx_amount_minor is None
    assert grocery.fx_currency is None
    assert grocery.fx_rate is None


def test_spec_parser_fx_line_that_does_not_match_falls_through_to_continuation() -> None:
    """A dateless row that doesn't match `fx` is still merged as a plain description
    continuation — `fx` only intercepts lines that actually match it."""
    words: list[Word] = []
    top = 100.0

    def add(row_words: list[Word]) -> None:
        nonlocal top
        words.extend(row_words)
        top += _ROW_HEIGHT + 2.0

    add(_heading("PURCHASES", top))
    add(_row(top, posted_date="01/05", description="Some Merchant", amount="10.00"))
    add(_row(top, description="Ref# 12345 extra detail"))
    doc = _doc(_PERIOD_TEXT, words)

    loaded = load_spec(_LAYOUT_B_SHAPED_SPEC, source="user_authored")
    parsed = loaded.parser.parse(doc)

    assert len(parsed.transactions) == 1
    assert parsed.transactions[0].description == "Some Merchant Ref# 12345 extra detail"
    assert parsed.transactions[0].fx_amount_minor is None


def test_load_spec_rejects_fx_pattern_that_is_pathological() -> None:
    text = """
id: evil_fx
version: 1
account_type: credit
detect: {score: 0.5}
fx: {pattern: "(a+)+", amount_group: 1, currency_group: 2, rate_group: 3}
columns:
  - {name: posted_date, x0: 0, x1: 50, type: date, formats: ["%m/%d"]}
  - {name: description, x0: 50, x1: 200, type: text}
  - {name: amount, x0: 200, x1: 260, type: money}
"""
    with pytest.raises(LayoutSpecError) as exc_info:
        load_spec(text, source="pasted")
    assert "backtrack" in str(exc_info.value)


def test_load_spec_rejects_fx_group_that_is_not_a_name_or_positive_index() -> None:
    text = """
id: bad_fx_group
version: 1
account_type: credit
detect: {score: 0.5}
fx: {pattern: "FX (.+)", amount_group: 0, currency_group: 2, rate_group: 3}
columns:
  - {name: posted_date, x0: 0, x1: 50, type: date, formats: ["%m/%d"]}
  - {name: description, x0: 50, x1: 200, type: text}
  - {name: amount, x0: 200, x1: 260, type: money}
"""
    with pytest.raises(LayoutSpecError) as exc_info:
        load_spec(text, source="pasted")
    assert any(e.field == "fx.amount_group" for e in exc_info.value.errors)


def test_spec_parser_fx_with_named_groups() -> None:
    text = r"""
id: named_fx
version: 1
account_type: credit
detect: {score: 0.5}
columns:
  - {name: posted_date, x0: 0, x1: 50, type: date, formats: ["%m/%d"]}
  - {name: description, x0: 50, x1: 200, type: text, multiline: true}
  - {name: amount, x0: 200, x1: 260, type: money}
sign: {outflow: unsigned, inflow: leading_minus}
year_inference: from_period
fx:
  pattern: '^FX (?P<amt>[\d.]+) (?P<cur>[A-Z]{3}) @ (?P<rate>[\d.]+)$'
  amount_group: amt
  currency_group: cur
  rate_group: rate
"""
    words: list[Word] = []
    top = 100.0

    def add(row_words: list[Word]) -> None:
        nonlocal top
        words.extend(row_words)
        top += _ROW_HEIGHT + 2.0

    add(_row(top, posted_date="01/05", description="Coffee Shop", amount="10.00"))
    add(_heading("FX 35.00 GBP @ 1.20000", top))
    doc = _doc(_PERIOD_TEXT, words)

    loaded = load_spec(text, source="pasted")
    parsed = loaded.parser.parse(doc)

    assert len(parsed.transactions) == 1
    assert parsed.transactions[0].fx_amount_minor == 3500
    assert parsed.transactions[0].fx_currency == "GBP"
    assert parsed.transactions[0].fx_rate == pytest.approx(1.2)


# --------------------------------------------------------------------------------------------
# Sign-dependent kind hints: outflow_kind_hint / inflow_kind_hint on a section pattern
# --------------------------------------------------------------------------------------------

_SIGN_DEPENDENT_KIND_SPEC = """
id: test_sign_dependent
version: 1
account_type: credit
detect: {score: 0.5}
columns:
  - {name: posted_date, x0: 0, x1: 50, type: date, formats: ["%m/%d"]}
  - {name: description, x0: 50, x1: 200, type: text}
  - {name: amount, x0: 200, x1: 260, type: money}
sections:
  mode: heading
  patterns:
    - {match: "PURCHASES", outflow_kind_hint: purchase, inflow_kind_hint: adjustment}
sign: {outflow: unsigned, inflow: leading_minus}
year_inference: from_period
"""


def test_sign_dependent_kind_hints_purchases_section_layout_a_and_b() -> None:
    """PURCHASES -> `purchase` for an outflow row, `adjustment` for an inflow row (a
    merchant-issued statement credit) — the convention Layout A and B both need."""
    words: list[Word] = []
    top = 100.0

    def add(row_words: list[Word]) -> None:
        nonlocal top
        words.extend(row_words)
        top += _ROW_HEIGHT + 2.0

    add(_heading("PURCHASES", top))
    add(_row(top, posted_date="01/05", description="Coffee Shop", amount="4.50"))
    add(_row(top, posted_date="01/06", description="Merchant Credit", amount="-15.00"))
    doc = _doc(_PERIOD_TEXT, words)

    loaded = load_spec(_SIGN_DEPENDENT_KIND_SPEC, source="user_authored")
    parsed = loaded.parser.parse(doc)

    assert len(parsed.transactions) == 2
    purchase, credit = parsed.transactions
    assert purchase.amount_minor == 450
    assert purchase.kind_hint == "purchase"
    assert credit.amount_minor == -1500
    assert credit.kind_hint == "adjustment"


def test_load_spec_rejects_section_pattern_with_no_kind_hint_at_all() -> None:
    text = """
id: no_kind_hint
version: 1
account_type: credit
detect: {score: 0.5}
columns:
  - {name: posted_date, x0: 0, x1: 50, type: date, formats: ["%m/%d"]}
  - {name: description, x0: 50, x1: 200, type: text}
  - {name: amount, x0: 200, x1: 260, type: money}
sections:
  mode: heading
  patterns:
    - {match: "PURCHASES"}
"""
    with pytest.raises(LayoutSpecError) as exc_info:
        load_spec(text, source="pasted")
    assert any(e.field == "sections.patterns[0]" for e in exc_info.value.errors)


# --------------------------------------------------------------------------------------------
# (name, version) immutability, revise(), resolve_spec(), build_parser_for_row()
# --------------------------------------------------------------------------------------------


def _make_issuer(session: Session, name: str = "Test Bank") -> Issuer:
    issuer = Issuer(name=name, slug=name.lower().replace(" ", "-"), match_terms="[]")
    session.add(issuer)
    session.flush()
    return issuer


def test_revise_inserts_version_plus_one_and_never_mutates(session: Session) -> None:
    loaded = load_spec(_LAYOUT_B_SHAPED_SPEC, source="user_authored")
    issuer = _make_issuer(session)

    v1 = revise(session, loaded, name="my_bank_credit", issuer_id=issuer.id, account_type="credit")
    assert v1.version == 1
    assert v1.parser_id == f"spec_{loaded.doc.id}_v1"
    v1_id, v1_yaml = v1.id, v1.spec_yaml

    revised_text = _LAYOUT_B_SHAPED_SPEC.replace("test_layout_b", "test_layout_b")  # same content
    loaded2 = load_spec(revised_text, source="user_authored")
    v2 = revise(session, loaded2, name="my_bank_credit", issuer_id=issuer.id, account_type="credit")
    session.commit()

    assert v2.version == 2
    assert v2.parser_id == f"spec_{loaded.doc.id}_v2"
    assert v2.id != v1_id

    # the v1 row is untouched
    reloaded_v1 = session.get(LayoutSpec, v1_id)
    assert reloaded_v1 is not None
    assert reloaded_v1.version == 1
    assert reloaded_v1.spec_yaml == v1_yaml


def test_resolve_spec_picks_highest_approved_version(session: Session) -> None:
    loaded = load_spec(_LAYOUT_B_SHAPED_SPEC, source="user_authored")
    issuer = _make_issuer(session, name="Another Bank")

    v1 = revise(
        session, loaded, name="another_bank_credit", issuer_id=issuer.id, account_type="credit"
    )
    v2 = revise(
        session, loaded, name="another_bank_credit", issuer_id=issuer.id, account_type="credit"
    )
    session.commit()

    # nothing approved yet -> no resolution
    assert resolve_spec(session, issuer_id=issuer.id, account_type="credit") is None

    approve_spec(session, v1)
    session.commit()
    resolved = resolve_spec(session, issuer_id=issuer.id, account_type="credit")
    assert resolved is not None
    assert resolved.version == 1

    approve_spec(session, v2)
    session.commit()
    resolved = resolve_spec(session, issuer_id=issuer.id, account_type="credit")
    assert resolved is not None
    assert resolved.version == 2


def test_build_parser_for_row_keeps_parser_id_and_version_in_agreement(session: Session) -> None:
    loaded = load_spec(_LAYOUT_B_SHAPED_SPEC, source="pasted")
    issuer = _make_issuer(session, name="Yet Another Bank")
    row = revise(
        session, loaded, name="yet_another_bank_credit", issuer_id=issuer.id, account_type="credit"
    )
    session.commit()

    parser = build_parser_for_row(row)
    assert parser.id == row.parser_id
    assert parser.version == str(row.version)

    parsed = parser.parse(_layout_b_shaped_doc())
    assert len(parsed.transactions) == 3
