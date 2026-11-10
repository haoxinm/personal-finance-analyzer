"""§6.6 performance budget: import a 12-page statement in < 3 s (extraction alone in < 2 s).

Opt-in only (`@pytest.mark.benchmark`, excluded from the default `pytest` run by
`[tool.pytest.ini_options] addopts` in `pyproject.toml`, P4-D) — timing assertions are inherently
flaky on shared/loaded CI runners, so they never gate the default suite; run explicitly with::

    uv run pytest -m benchmark tests/bench -v -s

The 12-page PDF is a synthetic, card-style statement (the same shape `tests/ingest/test_pipeline.py`
uses for the generic parser, at realistic statement volume: 30 transactions/page x 12 pages = 360
transactions) built at test time with the shared, frozen `tests.fixtures.gen.base` reportlab
helpers — never a real statement, and never written into the repository tree (§6.5).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from personal_finance_analyzer.db.models import Issuer, User
from personal_finance_analyzer.ingest import extract, pipeline
from tests.fixtures.gen.base import format_row, render_lines_pdf

pytestmark = pytest.mark.benchmark

_ROWS_PER_PAGE = 30
_PAGE_COUNT = 12
_TOTAL_ROWS = _ROWS_PER_PAGE * _PAGE_COUNT

_WIDTHS = (10, 32, 14)
_HEADER = format_row(["Date", "Description", "Amount"], _WIDTHS)

# Extraction budget (§6.6): "Extract a 12-page statement" < 2 s. Full import (extract + parse +
# dedupe + reconcile + persist, §6.6 "Full import of a 12-page statement") < 3 s.
_EXTRACT_BUDGET_S = 2.0
_IMPORT_BUDGET_S = 3.0


def _amount_str(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) / 100:.2f}"


def _build_12_page_statement(path: Path) -> int:
    """Render a synthetic 12-page, 360-row card-style statement. Returns the total in minor units
    (all purchases, so this is also the closing-balance delta from a zero opening balance)."""
    pages: list[list[str]] = []
    total_minor = 0
    row_index = 0
    for page_number in range(_PAGE_COUNT):
        lines: list[str] = []
        if page_number == 0:
            lines.extend(
                [
                    "Previous Balance: $0.00",
                    f"New Balance: ${_amount_str(_expected_total(_TOTAL_ROWS))}",
                    "Statement Period: 01/01/2026 to 01/31/2026",
                    "",
                ]
            )
        lines.append(_HEADER)
        for _ in range(_ROWS_PER_PAGE):
            day = 1 + (row_index % 28)
            amount_minor = 500 + (row_index % 97) * 13  # varied, always-positive purchase amounts
            total_minor += amount_minor
            # A short, single-word-ish description (as `tests/ingest/test_pipeline.py`'s own card
            # fixtures use): the generic parser prefers header-derived column bands (§ P1-B), and
            # a long, multi-word description can overflow past the midpoint between the header's
            # own short "Description"/"Amount" labels, which is a real limit of that heuristic
            # (P1-B/P2-A code, not owned here) rather than something this benchmark should probe.
            lines.append(
                format_row(
                    [
                        f"01/{day:02d}/2026",
                        f"MERCHANT{row_index:04d} US",
                        _amount_str(amount_minor),
                    ],
                    _WIDTHS,
                )
            )
            row_index += 1
        pages.append(lines)
    render_lines_pdf(path, pages)
    return total_minor


def _expected_total(n: int) -> int:
    """Precomputed so the closing balance line can be written before the rows are generated
    (both loops use the same deterministic `amount_minor` formula)."""
    return sum(500 + (i % 97) * 13 for i in range(n))


def _make_user(session: Session) -> User:
    user = User(name="bench-user")
    session.add(user)
    session.flush()
    return user


def _make_issuer(session: Session) -> Issuer:
    issuer = Issuer(name="Bench Issuer", slug="bench-issuer")
    session.add(issuer)
    session.flush()
    return issuer


def test_extract_12_page_statement_under_budget(tmp_path: Path) -> None:
    pdf_path = tmp_path / "bench_12_page.pdf"
    _build_12_page_statement(pdf_path)

    start = time.perf_counter()
    doc = extract.extract(pdf_path)
    elapsed = time.perf_counter() - start

    assert len(doc.pages) == _PAGE_COUNT
    print(
        f"\n[bench] extract 12-page statement: {elapsed * 1000:.1f} ms (budget {_EXTRACT_BUDGET_S * 1000:.0f} ms)"
    )
    assert elapsed < _EXTRACT_BUDGET_S, (
        f"extract took {elapsed:.3f}s, budget is {_EXTRACT_BUDGET_S}s"
    )


def test_full_import_12_page_statement_under_budget(session: Session, tmp_path: Path) -> None:
    pdf_path = tmp_path / "bench_12_page.pdf"
    _build_12_page_statement(pdf_path)
    user = _make_user(session)
    issuer = _make_issuer(session)

    start = time.perf_counter()
    proposal = pipeline.propose_import(
        session, pdf_path, user_id=user.id, original_name=pdf_path.name
    )
    session.commit()
    result = pipeline.confirm_import(
        session,
        proposal.statement_id,
        issuer_id=issuer.id,
        parser_id=proposal.parser_id,
        layout_spec_id=proposal.layout_spec_id,
        remember=True,
    )
    session.commit()
    elapsed = time.perf_counter() - start

    print(
        f"\n[bench] full import of 12-page/{_TOTAL_ROWS}-row statement: {elapsed * 1000:.1f} ms "
        f"(budget {_IMPORT_BUDGET_S * 1000:.0f} ms); inserted={result.inserted}"
    )
    assert result.inserted == _TOTAL_ROWS
    assert elapsed < _IMPORT_BUDGET_S, (
        f"full import took {elapsed:.3f}s, budget is {_IMPORT_BUDGET_S}s"
    )
