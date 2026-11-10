"""`ingest/tax/edit.py`: manual documents, forms and items, validation, provenance, summaries."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.analytics.tax_query import TaxQuery, run_tax_summary
from personal_finance_analyzer.db.models import IncomeItem, TaxDocument, TaxForm, TaxItem, User
from personal_finance_analyzer.ingest.tax import edit
from personal_finance_analyzer.ingest.tax.pipeline import import_tax_document
from tests.ingest.tax_support import generated_pdf


@pytest.fixture
def uid(session: Session) -> int:
    user = User(name="alice")
    session.add(user)
    session.commit()
    return user.id


def _w2(**kw: object) -> edit.FormData:
    base: dict[str, object] = {
        "form_type": "w2",
        "payer_name": "Acme Corp",
        "payer_tin_last4": "1234",
        "income_items": (edit.IncomeItemData("salary_wages", 100_000_00, box="1"),),
        "tax_items": (edit.TaxItemData("federal_income_tax_withheld", 10_000_00, box="2"),),
    }
    base.update(kw)
    return edit.FormData(**base)  # type: ignore[arg-type]


def _total(session: Session, user_id: int, year: int = 2024) -> int:
    rows = run_tax_summary(session, TaxQuery(user_ids=[user_id], tax_years=[year]))
    return rows[0].total_income_minor if rows else 0


def test_manual_document_has_no_file(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    assert doc.original_name == "Manual entry"
    assert doc.status == "parsed" and doc.parser_id == "manual"
    assert doc.stored_path is None and doc.page_count is None
    assert doc.file_sha256.startswith("manual:")
    other = edit.create_manual_document(session, uid, 2024, "again")
    assert other.file_sha256 != doc.file_sha256


@pytest.mark.parametrize("year", [1999, 2100])
def test_manual_document_rejects_bad_year(session: Session, uid: int, year: int) -> None:
    with pytest.raises(edit.Invalid):
        edit.create_manual_document(session, uid, year, None)


def test_manual_document_unknown_user(session: Session) -> None:
    with pytest.raises(edit.NotFound):
        edit.create_manual_document(session, 999, 2024, None)


def test_manual_name_is_scrubbed(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, "w2 123456789.pdf")
    assert "123456789" not in doc.original_name


def test_add_form_sets_provenance_and_unique_fingerprint(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    a = edit.add_form(session, doc.id, _w2())
    b = edit.add_form(session, doc.id, _w2())  # identical content: still separate forms
    assert a.fingerprint != b.fingerprint
    assert a.extraction_method == "manual" and a.is_user_edited is False
    assert a.role == "source" and a.tax_year == 2024
    assert {i.origin for i in a.income_items} == {"manual"}
    assert {t.origin for t in a.tax_items} == {"manual"}
    assert a.income_items[0].source_form == "W-2"
    assert a.income_items[0].tax_year == 2024


def test_add_form_year_defaults_and_mismatch(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    edit.add_form(session, doc.id, _w2())
    with pytest.raises(edit.YearMismatch):
        edit.add_form(session, doc.id, _w2(tax_year=2023))
    assert len(session.scalars(select(TaxForm)).all()) == 1


def test_add_form_validation_leaves_nothing_behind(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    bad = _w2(income_items=(edit.IncomeItemData("salary_wages", -1),))
    with pytest.raises(edit.Invalid):
        edit.add_form(session, doc.id, bad)
    session.rollback()
    assert session.scalars(select(TaxForm)).all() == []
    assert session.scalars(select(IncomeItem)).all() == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"payer_name": "123-45-6789"},
        {"payer_name": "123456789"},
        {"payer_tin_last4": "12345"},
        {"payer_tin_last4": "12a4"},
        {"payer_tin_last4": "١٢٣٤"},  # non-ASCII digits
        {"account_last4": "123"},
        {"form_type": "1099-x"},
        {"filing_status": "married"},
        {"taxable_income_minor": -1},
        {"agi_minor": 10**13},
    ],
)
def test_form_validation(session: Session, uid: int, kwargs: dict[str, object]) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    with pytest.raises(edit.Invalid):
        edit.add_form(session, doc.id, _w2(**kwargs))


def test_ein_in_payer_name_is_scrubbed_not_stored(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    form = edit.add_form(session, doc.id, _w2(payer_name="Acme 12-3456789"))
    assert form.payer_name is not None and "3456789" not in form.payer_name.replace("6789", "")
    assert "12-3456789" not in form.payer_name


def test_flips_error_document_to_parsed(session: Session, uid: int) -> None:
    doc = TaxDocument(
        user_id=uid, file_sha256="h", original_name="bad.pdf", status="error", error_detail="boom"
    )
    session.add(doc)
    session.commit()
    edit.add_form(session, doc.id, _w2(tax_year=2024))
    session.refresh(doc)
    assert doc.status == "parsed" and doc.error_detail is None and doc.tax_year == 2024
    assert json.loads(doc.warnings or "[]") == [edit.MANUAL_WARNING]
    edit.add_form(session, doc.id, _w2())
    session.refresh(doc)
    assert json.loads(doc.warnings or "[]") == [edit.MANUAL_WARNING]  # not duplicated


def test_year_required_when_document_has_none(session: Session, uid: int) -> None:
    doc = TaxDocument(user_id=uid, file_sha256="h", original_name="u.pdf", status="unsupported")
    session.add(doc)
    session.commit()
    with pytest.raises(edit.Invalid):
        edit.add_form(session, doc.id, _w2())


def test_update_form_marks_edited_keeps_fingerprint_and_recomputes_role(
    session: Session, uid: int
) -> None:
    path = generated_pdf("tax_w2", "normal")
    import_tax_document(session, path, user_id=uid, original_name=path.name)
    form = session.scalars(select(TaxForm)).one()
    fingerprint = form.fingerprint
    assert form.is_user_edited is False and form.extraction_method == "parser"
    updated = edit.update_form(session, form.id, {"payer_name": "Renamed Inc", "form_type": "1040"})
    assert updated.payer_name == "Renamed Inc"
    assert updated.role == "return" and updated.form_type == "1040"
    assert updated.is_user_edited is True
    assert updated.extraction_method == "parser"
    assert updated.fingerprint == fingerprint


def test_patch_null_clears_and_unknown_field_rejected(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    form = edit.add_form(session, doc.id, _w2())
    assert edit.update_form(session, form.id, {"payer_name": None}).payer_name is None
    assert edit.update_form(session, form.id, {"payer_name": "   "}).payer_name is None
    with pytest.raises(edit.Invalid):
        edit.update_form(session, form.id, {"fingerprint": "x"})
    with pytest.raises(edit.Invalid):
        edit.update_form(session, form.id, {"payer_name": "123-45-6789"})
    with pytest.raises(edit.Invalid):
        edit.update_form(session, form.id, {"form_type": None})
    with pytest.raises(edit.NotFound):
        edit.update_form(session, 9999, {"payer_name": "x"})


def test_changing_tax_year_moves_items_and_document(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    form = edit.add_form(session, doc.id, _w2())
    edit.update_form(session, form.id, {"tax_year": 2023})
    session.refresh(doc)
    assert doc.tax_year == 2023
    assert {i.tax_year for i in session.scalars(select(IncomeItem))} == {2023}
    assert {t.tax_year for t in session.scalars(select(TaxItem))} == {2023}
    assert _total(session, uid, 2023) == 100_000_00
    assert _total(session, uid, 2024) == 0


def test_changing_tax_year_moves_every_form_of_the_document(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    edit.add_form(session, doc.id, _w2())
    second = edit.add_form(session, doc.id, _w2(payer_name="Other Co", payer_tin_last4="9999"))
    edit.update_form(session, second.id, {"tax_year": 2023})
    session.refresh(doc)
    assert doc.tax_year == 2023
    assert {f.tax_year for f in session.scalars(select(TaxForm))} == {2023}
    assert {i.tax_year for i in session.scalars(select(IncomeItem))} == {2023}
    assert {t.tax_year for t in session.scalars(select(TaxItem))} == {2023}
    assert _total(session, uid, 2023) == 200_000_00
    assert _total(session, uid, 2024) == 0


def test_changing_tax_year_with_sibling_already_in_another_year_is_rejected(
    session: Session, uid: int
) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    edit.add_form(session, doc.id, _w2())
    second = edit.add_form(session, doc.id, _w2())
    other = session.scalars(select(TaxForm).where(TaxForm.id != second.id)).one()
    other.tax_year = 2022
    session.commit()
    with pytest.raises(edit.YearMismatch):
        edit.update_form(session, second.id, {"tax_year": 2023})


def test_multi_form_year_change_conflict_rolls_everything_back(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    first = edit.add_form(session, doc.id, _w2())
    second = edit.add_form(session, doc.id, _w2(payer_name="Other Co", payer_tin_last4="9999"))
    other_doc = edit.create_manual_document(session, uid, 2023, None)
    twin = edit.add_form(session, other_doc.id, _w2())
    twin.fingerprint = first.fingerprint
    session.commit()
    with pytest.raises(edit.Conflict):
        edit.update_form(session, second.id, {"tax_year": 2023})
    session.refresh(doc)
    assert doc.tax_year == 2024
    assert {
        f.tax_year for f in session.scalars(select(TaxForm).where(TaxForm.document_id == doc.id))
    } == {2024}


def test_changing_category_away_from_memo_drops_the_memo_flag(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    form = edit.add_form(
        session,
        doc.id,
        edit.FormData(
            "1099-div", income_items=(edit.IncomeItemData("qualified_dividends", 500_00),)
        ),
    )
    item = form.income_items[0]
    assert item.is_memo
    assert _total(session, uid) == 0
    edit.update_income_item(session, item.id, {"category": "ordinary_dividends", "is_memo": True})
    # an explicit flag from a client is honoured, but omitting it re-derives from the category
    edit.update_income_item(session, item.id, {"category": "qualified_dividends"})
    assert item.is_memo
    edit.update_income_item(session, item.id, {"category": "ordinary_dividends"})
    assert not item.is_memo
    assert _total(session, uid) == 500_00


def test_unchanged_category_keeps_a_parsed_memo_flag(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    form = edit.add_form(
        session,
        doc.id,
        edit.FormData(
            "1099-int", income_items=(edit.IncomeItemData("other", 50_00, is_memo=True),)
        ),
    )
    item = form.income_items[0]
    edit.update_income_item(session, item.id, {"amount_minor": 60_00})
    assert item.is_memo and item.amount_minor == 60_00


def test_year_change_colliding_with_existing_fingerprint_is_conflict(
    session: Session, uid: int
) -> None:
    path = generated_pdf("tax_w2", "normal")
    first = import_tax_document(session, path, user_id=uid, original_name=path.name)
    form = session.scalars(select(TaxForm)).one()
    year = form.tax_year
    # A second form with the same fingerprint in the neighbouring year.
    other_doc = edit.create_manual_document(session, uid, year - 1, None)
    twin = edit.add_form(session, other_doc.id, _w2())
    twin.fingerprint = form.fingerprint
    session.commit()
    assert first.status == "parsed"
    with pytest.raises(edit.Conflict):
        edit.update_form(session, twin.id, {"tax_year": year})


def test_delete_form_removes_items_and_updates_summary(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    form = edit.add_form(session, doc.id, _w2())
    assert _total(session, uid) == 100_000_00
    edit.delete_form(session, form.id)
    assert session.scalars(select(IncomeItem)).all() == []
    assert session.scalars(select(TaxItem)).all() == []
    assert _total(session, uid) == 0
    session.refresh(doc)
    assert doc.status == "parsed"  # the (now empty) document stays
    with pytest.raises(edit.NotFound):
        edit.delete_form(session, form.id)


def test_income_item_lifecycle(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    form = edit.add_form(session, doc.id, _w2())
    item = edit.add_income_item(session, form.id, edit.IncomeItemData("interest", 5_00))
    assert item.origin == "manual" and item.source_form == "W-2"
    assert _total(session, uid) == 100_000_00 + 5_00
    edit.update_income_item(session, item.id, {"amount_minor": 20_00, "label": "  Savings  "})
    session.refresh(item)
    assert item.amount_minor == 20_00 and item.label == "Savings"
    assert _total(session, uid) == 100_000_00 + 20_00
    edit.delete_income_item(session, item.id)
    assert _total(session, uid) == 100_000_00
    with pytest.raises(edit.NotFound):
        edit.update_income_item(session, item.id, {"amount_minor": 1})


def test_income_sign_rules(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    form = edit.add_form(session, doc.id, _w2())
    with pytest.raises(edit.Invalid):
        edit.add_income_item(session, form.id, edit.IncomeItemData("salary_wages", -1))
    with pytest.raises(edit.Invalid):
        edit.add_income_item(session, form.id, edit.IncomeItemData("nonsense", 1))
    loss = edit.add_income_item(session, form.id, edit.IncomeItemData("long_term_capital_gain", -5))
    assert loss.amount_minor == -5
    with pytest.raises(edit.Invalid):  # category change would make the loss illegal
        edit.update_income_item(session, loss.id, {"category": "interest"})
    with pytest.raises(edit.Invalid):
        edit.update_income_item(session, loss.id, {"amount_minor": 10**13})


def test_memo_category_forces_memo_and_is_excluded_from_total(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    form = edit.add_form(session, doc.id, _w2())
    memo = edit.add_income_item(session, form.id, edit.IncomeItemData("qualified_dividends", 7_00))
    assert memo.is_memo is True
    assert _total(session, uid) == 100_000_00


def test_labels_and_boxes_are_scrubbed_and_bounded(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    form = edit.add_form(session, doc.id, _w2())
    item = edit.add_income_item(
        session, form.id, edit.IncomeItemData("other", 1, label="acct 123-45-6789 x")
    )
    assert item.label is not None and "123-45-6789" not in item.label
    with pytest.raises(edit.Invalid):
        edit.add_income_item(session, form.id, edit.IncomeItemData("other", 1, label="x" * 81))
    with pytest.raises(edit.Invalid):
        edit.add_income_item(session, form.id, edit.IncomeItemData("other", 1, box="123456789"))


def test_tax_item_lifecycle_and_rules(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    form = edit.add_form(session, doc.id, _w2())
    with pytest.raises(edit.Invalid):
        edit.add_tax_item(session, form.id, edit.TaxItemData("medicare_tax", -1))
    with pytest.raises(edit.Invalid):
        edit.add_tax_item(session, form.id, edit.TaxItemData("bogus", 1))
    item = edit.add_tax_item(
        session, form.id, edit.TaxItemData("state_income_tax_withheld", 300, jurisdiction="ca")
    )
    assert item.origin == "manual" and item.jurisdiction == "ca"
    edit.update_tax_item(session, item.id, {"jurisdiction": None, "amount_minor": 400})
    session.refresh(item)
    assert item.jurisdiction is None and item.amount_minor == 400
    rows = run_tax_summary(session, TaxQuery(user_ids=[uid], tax_years=[2024]))
    assert sum(a for a in rows[0].withheld.values()) == 10_000_00 + 400
    edit.delete_tax_item(session, item.id)
    rows = run_tax_summary(session, TaxQuery(user_ids=[uid], tax_years=[2024]))
    assert sum(a for a in rows[0].withheld.values()) == 10_000_00


def test_item_edits_mark_form_edited(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    form = edit.add_form(session, doc.id, _w2())
    assert form.is_user_edited is False
    edit.add_income_item(session, form.id, edit.IncomeItemData("other", 1))
    session.refresh(form)
    assert form.is_user_edited is True


def test_edited_import_item_becomes_manual(session: Session, uid: int) -> None:
    path = generated_pdf("tax_w2", "normal")
    import_tax_document(session, path, user_id=uid, original_name=path.name)
    item = session.scalars(select(IncomeItem)).first()
    assert item is not None and item.origin == "parser"
    edit.update_income_item(session, item.id, {"amount_minor": 1_00})
    session.refresh(item)
    assert item.origin == "manual"


def test_reimport_after_edit_is_still_deduped(session: Session, uid: int) -> None:
    path = generated_pdf("tax_w2", "normal")
    import_tax_document(session, path, user_id=uid, original_name=path.name)
    form = session.scalars(select(TaxForm)).one()
    edit.update_form(session, form.id, {"payer_name": "Edited"})
    again = import_tax_document(session, path, user_id=uid, original_name="copy.pdf")
    assert again.status == "duplicate" or again.inserted_forms == 0
    assert len(session.scalars(select(TaxForm)).all()) == 1


def test_confirm_marks_llm_items_manual(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    form = edit.add_form(session, doc.id, _w2())
    for item in (*form.income_items, *form.tax_items):
        item.origin = "llm"
    form.extraction_method = "llm"
    session.commit()
    done = edit.confirm_form(session, form.id)
    assert {i.origin for i in done.income_items} == {"manual"}
    assert {t.origin for t in done.tax_items} == {"manual"}
    assert done.extraction_method == "llm"


def test_no_full_identifier_is_ever_stored(session: Session, uid: int) -> None:
    doc = edit.create_manual_document(session, uid, 2024, None)
    for bad in ("123-45-6789", "12-3456789", "123456789"):
        with pytest.raises(edit.Invalid):
            edit.add_form(session, doc.id, _w2(payer_name=bad))
        with pytest.raises(edit.Invalid):
            edit.add_form(session, doc.id, _w2(payer_tin_last4=bad))
        with pytest.raises(edit.Invalid):
            edit.add_form(session, doc.id, _w2(account_last4=bad))
    assert session.scalars(select(TaxForm)).all() == []
