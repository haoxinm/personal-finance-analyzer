"""Regression tests for review findings on re-import and reassign (corrections-aware)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from personal_finance_analyzer.api.services import crud
from personal_finance_analyzer.core.errors import ParserError, PfaError, UnsupportedLayoutError
from personal_finance_analyzer.db.models import Account, Statement, Transaction
from personal_finance_analyzer.ingest import pipeline
from personal_finance_analyzer.ingest.dedupe import DedupeKey, compute_dedupe_hash
from tests.ingest.test_pipeline import (
    _build_card_pdf,
    _edit_by_hand,
    _import_three_rows,
    _make_issuer,
    _make_user,
    _propose_and_confirm,
    _rows,
)


@pytest.mark.parametrize("exc", [ParserError("bad year"), UnsupportedLayoutError("no layout")])
def test_failed_reparse_leaves_a_parsed_statement_untouched(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: PfaError
) -> None:
    statement_id, _ = _import_three_rows(session, tmp_path, "fail")
    _edit_by_hand(session, _rows(session, statement_id)["COFFEE SHOP"], "travel", "hotels")
    before = session.get(Statement, statement_id)
    assert before is not None
    pages_before = before.selected_pages
    ids_before = sorted(
        t.id for t in session.query(Transaction).filter_by(statement_id=statement_id)
    )

    def boom(self: object, doc: object) -> None:
        raise exc

    monkeypatch.setattr(type(pipeline.generic_table), "parse", boom)
    with pytest.raises(PfaError):
        pipeline.reparse(
            session, statement_id, parser_id="generic_table", layout_spec_id=None, pages=[1]
        )
    session.commit()
    session.expire_all()

    st = session.get(Statement, statement_id)
    assert st is not None
    assert st.status == "parsed" and st.error_detail is None
    assert st.selected_pages == pages_before
    ids_after = sorted(
        t.id for t in session.query(Transaction).filter_by(statement_id=statement_id)
    )
    assert ids_after == ids_before


def _live_total(session: Session, statement: Statement) -> int:
    """The statement's rows as they are now: the parsed total plus the live adjustment."""
    assert statement.parsed_total_minor is not None
    return statement.parsed_total_minor + crud.statement_adjustments(session, [statement.id]).get(
        statement.id, 0
    )


def _bob_twin(session: Session, source: Transaction, issuer_id: int) -> tuple[Account, Transaction]:
    """Bob's account already holds the very same *imported* transaction (a true duplicate)."""
    bob = _make_user(session, "bob")
    target = Account(
        user_id=bob.id, issuer_id=issuer_id, account_type="credit", display_name="B", mask="9999"
    )
    session.add(target)
    session.flush()
    st = Statement(
        file_sha256="1" * 64,
        original_name="bob.pdf",
        status="parsed",
        ingested_at="now",
        parsed_total_minor=source.orig_amount_minor or source.amount_minor,
    )
    session.add(st)
    session.flush()
    imported_date = source.orig_posted_date or source.posted_date
    imported_amount = source.orig_amount_minor or source.amount_minor
    twin = Transaction(
        statement_id=st.id,
        account_id=target.id,
        user_id=bob.id,
        posted_date=imported_date,
        description_raw=source.description_raw,
        description_clean=source.description_clean,
        merchant_key=source.merchant_key,
        amount_minor=imported_amount,
        currency=source.currency,
        kind=source.kind,
        is_spend=source.is_spend,
        dedupe_hash=compute_dedupe_hash(
            DedupeKey(
                account_id=target.id,
                posted_date=imported_date,
                amount_minor=imported_amount,
                description_raw=source.description_raw,
            ),
            0,
        ),
    )
    session.add(twin)
    session.commit()
    return target, twin


def _setup_corrected_coffee(session: Session, tmp_path: Path) -> tuple[int, int, int]:
    pdf_path = tmp_path / "s.pdf"
    _build_card_pdf(
        pdf_path,
        [
            ("01/03/2026", "GROCERY MART", "45.10"),
            ("01/05/2026", "COFFEE SHOP", "12.34"),
            ("01/09/2026", "ONLINE BOOKSTORE", "29.99"),
        ],
        closing="87.43",
    )
    user = _make_user(session)
    issuer = _make_issuer(session, "Dup Bank")
    proposal, _ = _propose_and_confirm(session, pdf_path, user_id=user.id, issuer_id=issuer.id)
    return proposal.statement_id, issuer.id, user.id


def test_reassign_hash_and_duplicates_use_imported_not_corrected_values(
    session: Session, tmp_path: Path
) -> None:
    statement_id, issuer_id, _ = _setup_corrected_coffee(session, tmp_path)
    coffee = _rows(session, statement_id)["COFFEE SHOP"]
    crud.apply_field_corrections(
        session, coffee, posted_date="2026-01-06", description_clean=None, amount_minor=1500
    )
    session.commit()
    target, twin = _bob_twin(session, coffee, issuer_id)
    statement = session.get(Statement, statement_id)
    assert statement is not None
    moved_total_before = _live_total(session, statement)
    twin_statement = session.get(Statement, twin.statement_id)
    assert twin_statement is not None
    twin_total_before = _live_total(session, twin_statement)

    moved, deleted = pipeline.reassign(
        session, statement_id, user_id=target.user_id, account_id=target.id
    )
    session.commit()
    session.expire_all()

    assert (moved, deleted) == (2, 1)
    survivor = session.get(Transaction, twin.id)
    assert survivor is not None
    # The correction travels to the survivor, with the imported value kept as orig_*.
    assert (survivor.amount_minor, survivor.orig_amount_minor) == (1500, 1234)
    assert (survivor.posted_date, survivor.orig_posted_date) == ("2026-01-06", "2026-01-05")
    # Totals: the survivor's statement moves by the correction exactly once; the moved statement
    # no longer carries the corrected amount of the row that was deleted.
    twin_statement = session.get(Statement, twin.statement_id)
    statement = session.get(Statement, statement_id)
    assert twin_statement is not None and statement is not None
    assert _live_total(session, twin_statement) == twin_total_before + (1500 - 1234)
    assert _live_total(session, statement) == moved_total_before - (1500 - 1234)


def test_reassign_moved_corrected_row_keeps_its_hash_when_there_is_no_twin(
    session: Session, tmp_path: Path
) -> None:
    statement_id, issuer_id, _ = _setup_corrected_coffee(session, tmp_path)
    coffee = _rows(session, statement_id)["COFFEE SHOP"]
    crud.apply_field_corrections(
        session, coffee, posted_date=None, description_clean=None, amount_minor=1500
    )
    session.commit()
    bob = _make_user(session, "bob2")
    target = Account(
        user_id=bob.id, issuer_id=issuer_id, account_type="credit", display_name="C", mask="1"
    )
    session.add(target)
    session.flush()
    pipeline.reassign(session, statement_id, user_id=bob.id, account_id=target.id)
    session.commit()
    session.expire_all()
    moved = session.get(Transaction, coffee.id)
    assert moved is not None
    expected = compute_dedupe_hash(
        DedupeKey(
            account_id=target.id,
            posted_date="2026-01-05",
            amount_minor=1234,
            description_raw=moved.description_raw,
        ),
        0,
    )
    assert moved.dedupe_hash == expected


def test_reassign_hands_corrections_to_a_survivor_with_a_category_edit(
    session: Session, tmp_path: Path
) -> None:
    statement_id, issuer_id, _ = _setup_corrected_coffee(session, tmp_path)
    coffee = _rows(session, statement_id)["COFFEE SHOP"]
    crud.apply_field_corrections(
        session, coffee, posted_date=None, description_clean="Latte", amount_minor=None
    )
    session.commit()
    target, twin = _bob_twin(session, coffee, issuer_id)
    _edit_by_hand(session, twin, "travel", "hotels")
    session.refresh(twin)
    category = (twin.category_id, twin.subcategory_id)

    pipeline.reassign(session, statement_id, user_id=target.user_id, account_id=target.id)
    session.commit()
    session.expire_all()
    survivor = session.get(Transaction, twin.id)
    assert survivor is not None
    assert survivor.classified_by == "user"
    assert (survivor.category_id, survivor.subcategory_id) == category
    assert survivor.description_clean == "Latte"  # correction carried over, not lost


def test_a_user_edit_never_overwrites_the_true_imported_value(
    session: Session, tmp_path: Path
) -> None:
    statement_id, _ = _import_three_rows(session, tmp_path, "orig")
    coffee = _rows(session, statement_id)["COFFEE SHOP"]
    crud.apply_field_corrections(
        session, coffee, posted_date=None, description_clean=None, amount_minor=1500
    )
    session.commit()
    edit = pipeline._user_edit_of(coffee)
    pipeline._apply_user_edit(coffee, replace(edit, fix_amount_minor=1700))
    assert (coffee.amount_minor, coffee.orig_amount_minor) == (1700, 1234)
    session.flush()
    statement = coffee.statement
    assert _live_total(session, statement) == 1234 + 1700 - 1234 + (4510 + 2999)


def test_reparse_twin_correction_keeps_the_imported_value_as_original(
    session: Session, tmp_path: Path
) -> None:
    pdf_path = tmp_path / "tw.pdf"
    _build_card_pdf(
        pdf_path,
        [
            ("01/05/2026", "COFFEE SHOP", "12.34"),
            ("01/05/2026", "COFFEE SHOP", "12.34"),
            ("01/06/2026", "BAR", "1.00"),
        ],
        closing="25.68",
    )
    user = _make_user(session)
    issuer = _make_issuer(session, "Twin Bank")
    proposal, _ = _propose_and_confirm(session, pdf_path, user_id=user.id, issuer_id=issuer.id)
    rows = sorted(
        session.query(Transaction).filter_by(statement_id=proposal.statement_id), key=lambda t: t.id
    )
    crud.apply_field_corrections(
        session, rows[1], posted_date=None, description_clean=None, amount_minor=999
    )
    session.commit()
    pipeline.reparse(session, proposal.statement_id, parser_id="generic_table", layout_spec_id=None)
    session.commit()
    session.expire_all()
    for t in session.query(Transaction).filter_by(statement_id=proposal.statement_id):
        assert t.orig_amount_minor in (None, 1234)  # never the corrected value
