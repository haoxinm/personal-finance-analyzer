"""`import_tax_document`: persistence, idempotency, dedupe, failure statuses, PDF retention."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from spend_analyzer.config import PrivacyConfig, Settings, save_settings
from spend_analyzer.core.paths import extract_cache_dir, statements_dir
from spend_analyzer.db.models import IncomeItem, TaxDocument, TaxForm, TaxItem, User
from spend_analyzer.ingest.tax.pipeline import (
    UNSUPPORTED_MESSAGE,
    delete_tax_document,
    import_tax_document,
)
from tests.ingest.tax_support import generated_pdf

_SSN_SHAPE = re.compile(r"\d{3}-\d{2}-\d{4}")


def _user(session: Session, name: str = "alice") -> int:
    user = User(name=name)
    session.add(user)
    session.flush()
    return user.id


def _import(session: Session, layout: str, variant: str, user_id: int, **kw: int | None):  # type: ignore[no-untyped-def]
    path = generated_pdf(layout, variant)
    return import_tax_document(session, path, user_id=user_id, original_name=path.name, **kw)


_PARSE_CASES = [
    ("tax_w2", "normal", ("w2",)),
    ("tax_w2", "two_employers", ("w2", "w2")),
    ("tax_1099_int", "normal", ("1099-int",)),
    ("tax_1099_div", "normal", ("1099-div",)),
    ("tax_1099_b", "normal_summary", ("1099-b",)),
    ("tax_1099_nec_misc", "nec_normal", ("1099-nec",)),
    ("tax_1099_consolidated", "int_div_b", None),
    ("tax_1040", "with_investment_income", ("1040",)),
    ("tax_w2", "side_by_side_2up", ("w2", "w2")),
    ("tax_1099_int", "two_payers_stacked", ("1099-int", "1099-int")),
    ("tax_1099_broker", "two_up", ("1099-consolidated",)),
    ("tax_mixed_package", "w2_int_div", ("w2", "1099-int", "1099-div")),
]


@pytest.mark.parametrize(("layout", "variant", "forms"), _PARSE_CASES)
def test_imports_each_fixture_type(
    session: Session, layout: str, variant: str, forms: tuple[str, ...] | None
) -> None:
    uid = _user(session)
    result = _import(session, layout, variant, uid)
    assert result.status == "parsed", result.error
    assert result.error is None
    assert result.tax_year is not None
    assert result.inserted_forms == len(result.form_types) >= 1
    if forms is not None:
        assert result.form_types == forms
    doc = session.get(TaxDocument, result.document_id)
    assert doc is not None
    assert (doc.status, doc.user_id, doc.tax_year) == ("parsed", uid, result.tax_year)
    assert doc.parser_id and doc.parser_version
    n_income = session.scalar(select(func.count()).select_from(IncomeItem))
    n_tax = session.scalar(select(func.count()).select_from(TaxItem))
    assert (n_income, n_tax) == (result.income_item_count, result.tax_item_count)
    roles = set(session.scalars(select(TaxForm.role)))
    assert roles == ({"return"} if layout == "tax_1040" else {"source"})
    # Denormalised columns on items match their form.
    assert (
        session.scalar(
            select(func.count())
            .select_from(IncomeItem)
            .join(TaxForm, TaxForm.id == IncomeItem.form_id)
            .where(
                (IncomeItem.user_id != TaxForm.user_id) | (IncomeItem.tax_year != TaxForm.tax_year)
            )
        )
        == 0
    )


def test_w2_values_round_trip(session: Session) -> None:
    uid = _user(session)
    _import(session, "tax_w2", "normal", uid)
    (form,) = session.scalars(select(TaxForm)).all()
    assert form.payer_name == "Pinecrest Logistics LLC"
    assert form.payer_tin_last4 == "0001"
    wages, deferral, hsa = session.scalars(select(IncomeItem).order_by(IncomeItem.id)).all()
    assert (wages.category, wages.amount_minor) == ("salary_wages", 8_543_210)
    assert (deferral.category, deferral.amount_minor, deferral.is_memo) == (
        "retirement_deferral",
        320_000,
        True,
    )
    assert (hsa.category, hsa.amount_minor, hsa.box) == ("hsa_employer", 125_000, "12-W")


def test_duplicate_file_is_a_noop(session: Session) -> None:
    uid = _user(session)
    first = _import(session, "tax_w2", "normal", uid)
    second = _import(session, "tax_w2", "normal", uid)
    assert second.status == "duplicate"
    assert second.document_id == first.document_id
    assert second.form_types == first.form_types
    assert second.income_item_count == first.income_item_count
    assert second.inserted_forms == 0
    assert session.scalar(select(func.count()).select_from(TaxDocument)) == 1
    assert session.scalar(select(func.count()).select_from(TaxForm)) == 1


def test_same_form_in_two_different_pdfs_dedupes_by_fingerprint(
    session: Session, tmp_path: Path
) -> None:
    """Byte-different files (different sha) carrying the same W-2 insert the form once."""
    uid = _user(session)
    first = _import(session, "tax_w2", "normal", uid)
    variant = tmp_path / "w2_other_delivery.pdf"
    variant.write_bytes(generated_pdf("tax_w2", "normal").read_bytes() + b"\n% re-delivered\n")
    second = import_tax_document(session, variant, user_id=uid, original_name=variant.name)
    assert first.status == second.status == "parsed"
    assert first.document_id != second.document_id
    assert first.inserted_forms == 1
    assert second.inserted_forms == 0
    assert second.skipped_duplicate_forms == 1
    assert second.income_item_count == second.tax_item_count == 0
    assert any("already imported" in w for w in second.warnings)
    # The warning names the earlier file; `skipped_from` carries the same facts structurally.
    assert any(f'"{generated_pdf("tax_w2", "normal").name}"' in w for w in second.warnings)
    assert second.skipped_from == (
        (first.document_id, generated_pdf("tax_w2", "normal").name, "w2"),
    )
    assert first.skipped_from == ()
    assert session.scalar(select(func.count()).select_from(TaxForm)) == 1
    assert session.scalar(select(func.count()).select_from(IncomeItem)) == 3  # wages + box 12
    # Deleting the second (empty) document leaves the first document's data intact.
    assert delete_tax_document(session, second.document_id) is True
    assert session.scalar(select(func.count()).select_from(TaxForm)) == 1


def test_copies_variant_imports_a_single_form(session: Session) -> None:
    result = _import(session, "tax_w2", "copies", _user(session))
    assert (result.status, result.form_types) == ("parsed", ("w2",))


def test_same_form_for_two_users_is_not_a_duplicate(session: Session) -> None:
    a, b = _user(session, "alice"), _user(session, "bob")
    first = _import(session, "tax_w2", "normal", a)
    other = _import(session, "tax_w2", "copies", b)
    assert first.inserted_forms == other.inserted_forms == 1
    assert session.scalar(select(func.count()).select_from(TaxForm)) == 2


def test_non_tax_pdf_is_unsupported(session: Session) -> None:
    uid = _user(session)
    result = _import(session, "layout_d_bank", "normal", uid)
    assert result.status == "unsupported"
    assert result.error == UNSUPPORTED_MESSAGE
    assert result.inserted_forms == 0
    doc = session.get(TaxDocument, result.document_id)
    assert doc is not None
    assert (doc.status, doc.error_detail) == ("unsupported", UNSUPPORTED_MESSAGE)
    assert session.scalar(select(func.count()).select_from(TaxForm)) == 0


def test_parse_error_is_recorded_with_generic_message(session: Session) -> None:
    uid = _user(session)
    result = _import(session, "tax_w2", "no_box1", uid)
    assert result.status == "error"
    assert result.error
    assert not _SSN_SHAPE.search(result.error)
    assert not re.search(r"\d{6}", result.error)
    doc = session.get(TaxDocument, result.document_id)
    assert doc is not None and doc.status == "error"
    assert doc.error_detail == result.error


def test_tax_year_override_and_retry_of_failed_document(session: Session) -> None:
    uid = _user(session)
    failed = _import(session, "tax_w2", "no_year", uid)
    assert failed.status == "error"
    retried = _import(session, "tax_w2", "no_year", uid, tax_year=2024)
    assert retried.status == "parsed"
    assert retried.tax_year == 2024
    assert retried.document_id == failed.document_id  # the failed row is reused
    doc = session.get(TaxDocument, retried.document_id)
    assert doc is not None and doc.status == "parsed" and doc.error_detail is None
    assert session.scalar(select(func.count()).select_from(TaxDocument)) == 1


def test_no_text_layer_is_unsupported(session: Session, tmp_path: Path) -> None:
    from reportlab.pdfgen import canvas

    pdf = tmp_path / "scan.pdf"
    c = canvas.Canvas(str(pdf), invariant=1)
    c.setFillGray(0.6)
    c.rect(50, 500, 400, 200, fill=1, stroke=0)
    c.showPage()
    c.save()
    uid = _user(session)
    result = import_tax_document(session, pdf, user_id=uid, original_name="scan.pdf")
    assert result.status == "unsupported"
    assert result.error is not None and "text layer" in result.error


def test_pdf_not_retained_by_default(session: Session) -> None:
    uid = _user(session)
    result = _import(session, "tax_w2", "normal", uid)
    doc = session.get(TaxDocument, result.document_id)
    assert doc is not None and doc.stored_path is None
    kept = list(statements_dir().glob("*.pdf")) if statements_dir().exists() else []
    assert kept == []


def test_staged_upload_is_removed_when_not_retained(session: Session) -> None:
    """The API saves uploads at statements_dir()/<sha>.pdf; the pipeline must delete that copy."""
    from spend_analyzer.core.paths import ensure_home
    from spend_analyzer.ingest.extract import sha256_file

    ensure_home()
    src = generated_pdf("tax_w2", "normal")
    staged = statements_dir() / f"{sha256_file(src)}.pdf"
    staged.write_bytes(src.read_bytes())
    uid = _user(session)
    result = import_tax_document(session, staged, user_id=uid, original_name="w2.pdf")
    assert result.status == "parsed"
    assert not staged.exists()
    assert src.exists()  # a caller's own file is never touched


def test_pdf_retained_when_enabled_and_deleted_with_document(session: Session) -> None:
    save_settings(Settings(privacy=PrivacyConfig(store_tax_pdf_copies=True)))
    uid = _user(session)
    result = _import(session, "tax_w2", "normal", uid)
    doc = session.get(TaxDocument, result.document_id)
    assert doc is not None and doc.stored_path is not None
    stored = Path(doc.stored_path)
    assert stored.exists()
    assert delete_tax_document(session, result.document_id) is True
    assert not stored.exists()
    assert session.scalar(select(func.count()).select_from(TaxForm)) == 0
    assert session.scalar(select(func.count()).select_from(IncomeItem)) == 0
    assert session.scalar(select(func.count()).select_from(TaxItem)) == 0


def test_delete_unknown_document_returns_false(session: Session) -> None:
    assert delete_tax_document(session, 424242) is False


def test_extract_cache_never_written_even_when_enabled(session: Session) -> None:
    save_settings(Settings(privacy=PrivacyConfig(store_extract_cache=True)))
    uid = _user(session)
    _import(session, "tax_w2", "normal", uid)
    cache = extract_cache_dir()
    assert not cache.exists() or list(cache.iterdir()) == []


def test_logs_contain_only_ids_and_counts(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    uid = _user(session)
    _import(session, "tax_w2", "normal", uid)
    _import(session, "tax_w2", "no_box1", uid)
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("spend_analyzer"))
    assert "tax import" in text
    assert "Pinecrest" not in text
    assert "8543210" not in text and "85,432" not in text
    assert not _SSN_SHAPE.search(text)
    assert json.dumps(text)  # sanity: printable


# --- per-user dedupe, scrubbed names, unexpected failures, empty re-imports -------------------


def test_same_file_for_two_users_is_imported_for_each(session: Session) -> None:
    a, b = _user(session, "alice"), _user(session, "bob")
    first = _import(session, "tax_w2", "normal", a)
    second = _import(session, "tax_w2", "normal", b)
    assert (first.status, second.status) == ("parsed", "parsed")
    assert first.document_id != second.document_id
    assert second.inserted_forms == 1
    docs = {d.id: d.user_id for d in session.scalars(select(TaxDocument))}
    assert docs == {first.document_id: a, second.document_id: b}  # nobody is reassigned
    # the same user importing it again is still a no-op
    again = _import(session, "tax_w2", "normal", b)
    assert again.status == "duplicate" and again.document_id == second.document_id
    assert session.scalar(select(func.count()).select_from(TaxDocument)) == 2


def test_deleting_one_users_document_keeps_the_shared_kept_copy(session: Session) -> None:
    save_settings(Settings(privacy=PrivacyConfig(store_tax_pdf_copies=True)))
    a, b = _user(session, "alice"), _user(session, "bob")
    first = _import(session, "tax_w2", "normal", a)
    second = _import(session, "tax_w2", "normal", b)
    doc_b = session.get(TaxDocument, second.document_id)
    assert doc_b is not None and doc_b.stored_path is not None
    assert delete_tax_document(session, first.document_id)
    assert Path(doc_b.stored_path).exists()
    assert delete_tax_document(session, second.document_id)
    assert not Path(doc_b.stored_path).exists()


def test_original_name_is_scrubbed(session: Session) -> None:
    uid = _user(session)
    path = generated_pdf("tax_w2", "normal")
    result = import_tax_document(
        session, path, user_id=uid, original_name="W2 123-45-6789 acct 98765432101.pdf"
    )
    doc = session.get(TaxDocument, result.document_id)
    assert doc is not None
    assert "123-45" not in doc.original_name
    assert not re.search(r"\d{6,}", doc.original_name)
    assert "6789" in doc.original_name  # last four only
    assert doc.original_name.endswith(".pdf")


def _stage(src: Path) -> Path:
    from spend_analyzer.core.paths import ensure_home
    from spend_analyzer.ingest.extract import sha256_file

    ensure_home()
    staged = statements_dir() / f"{sha256_file(src)}.pdf"
    staged.write_bytes(src.read_bytes())
    return staged


@pytest.mark.parametrize("where", ["extract", "parse"])
def test_unexpected_exception_is_recorded_unstaged_and_not_raised(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    where: str,
) -> None:
    from spend_analyzer.ingest.tax import pipeline

    secret = "SSN 123-45-6789 leaked"

    def boom(*_a: object, **_k: object) -> object:
        raise RuntimeError(secret)

    if where == "extract":
        monkeypatch.setattr(pipeline, "extract", boom)
    else:
        monkeypatch.setattr(pipeline, "parse_document", boom)
    caplog.set_level(logging.DEBUG)
    staged = _stage(generated_pdf("tax_w2", "normal"))
    uid = _user(session)
    session.commit()  # a real request has committed its user; the failure path rolls back
    result = import_tax_document(session, staged, user_id=uid, original_name="w2.pdf")
    assert result.status == "error"
    assert result.error == pipeline.GENERIC_ERROR_MESSAGE
    assert not staged.exists()
    doc = session.get(TaxDocument, result.document_id)
    assert doc is not None and doc.status == "error"
    assert doc.error_detail == pipeline.GENERIC_ERROR_MESSAGE and doc.stored_path is None
    assert session.scalar(select(func.count()).select_from(TaxForm)) == 0
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in text and "123-45" not in text and "leaked" not in text


def test_unexpected_exception_mid_insert_rolls_back_partial_rows(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spend_analyzer.ingest.tax import pipeline

    monkeypatch.setattr(
        pipeline, "_form_fingerprint", lambda *a, **k: (_ for _ in ()).throw(KeyError("x"))
    )
    uid = _user(session)
    session.commit()
    result = _import(session, "tax_w2", "two_employers", uid)
    assert result.status == "error"
    assert session.scalar(select(func.count()).select_from(TaxForm)) == 0


def test_parsed_document_with_no_forms_is_reimported(session: Session) -> None:
    a = _user(session)
    first = _import(session, "tax_w2", "normal", a)
    assert first.status == "parsed"
    # Simulate "all forms were deduped into another document that has since been deleted".
    for form in session.scalars(select(TaxForm)).all():
        session.delete(form)
    session.commit()
    again = _import(session, "tax_w2", "normal", a)
    assert again.status == "parsed"
    assert again.document_id == first.document_id
    assert again.inserted_forms == 1
    assert session.scalar(select(func.count()).select_from(TaxForm)) == 1


def _redelivered(src: Path, tmp_path: Path, name: str) -> Path:
    """A byte-different copy of `src` (different file hash, same content)."""
    out = tmp_path / name
    out.write_bytes(src.read_bytes() + b"\n% re-delivered\n")
    return out


def _form_counts(session: Session) -> dict[int, int]:
    rows = session.execute(
        select(TaxForm.document_id, func.count()).group_by(TaxForm.document_id)
    ).all()
    return {int(doc_id): int(n) for doc_id, n in rows}


def test_forms_without_identity_from_two_files_are_both_kept(session: Session) -> None:
    """Two different payers, same amounts, no payer TIN and no readable account: weak identity,
    so the second file's form is not dropped as a "duplicate" of the first."""
    uid = _user(session)
    first = _import(session, "tax_1099_consolidated", "noid_a", uid)
    second = _import(session, "tax_1099_consolidated", "noid_b", uid)
    assert (first.status, second.status) == ("parsed", "parsed")
    assert (first.inserted_forms, second.inserted_forms) == (1, 1)
    assert second.skipped_duplicate_forms == 0 and second.skipped_from == ()
    assert not any("already imported" in w for w in second.warnings)
    assert _form_counts(session) == {first.document_id: 1, second.document_id: 1}
    assert set(session.scalars(select(TaxForm.payer_name))) == {
        "Alder Brokerage Group",
        "Birchwood Securities",
    }


def test_forms_with_no_payer_and_equal_amounts_in_two_files_are_both_kept(
    session: Session, tmp_path: Path
) -> None:
    uid = _user(session)
    src = generated_pdf("tax_1099_consolidated", "noid_anon")
    first = import_tax_document(session, src, user_id=uid, original_name="anon_1.pdf")
    other = _redelivered(src, tmp_path, "anon_2.pdf")
    second = import_tax_document(session, other, user_id=uid, original_name=other.name)
    assert (first.inserted_forms, second.inserted_forms) == (1, 1)
    assert second.skipped_duplicate_forms == 0
    assert session.scalar(select(func.count()).select_from(TaxForm)) == 2
    (note,) = [w for w in second.warnings if w.startswith("Looks identical")]
    assert '"anon_1.pdf"' in note and "Delete one" in note
    assert not any(w.startswith("Looks identical") for w in first.warnings)


def test_weak_named_form_reimported_from_another_pdf_is_skipped(
    session: Session, tmp_path: Path
) -> None:
    """No TIN or account, but the same payer name and identical amounts: the same form delivered
    in a different PDF is stored once, and the warning names the earlier file."""
    uid = _user(session)
    src = generated_pdf("tax_1099_consolidated", "noid_a")
    first = import_tax_document(session, src, user_id=uid, original_name="broker_a_1.pdf")
    other = _redelivered(src, tmp_path, "broker_a_2.pdf")
    second = import_tax_document(session, other, user_id=uid, original_name="broker_a_2.pdf")
    assert (first.inserted_forms, second.inserted_forms) == (1, 0)
    assert second.skipped_duplicate_forms == 1
    assert any('"broker_a_1.pdf"' in w and "skipped" in w for w in second.warnings)
    assert session.scalar(select(func.count()).select_from(TaxForm)) == 1
    # Another provider (different name) with the same amounts is never dropped.
    third = _import(session, "tax_1099_consolidated", "noid_b", uid)
    assert third.inserted_forms == 1 and third.skipped_duplicate_forms == 0
    assert session.scalar(select(func.count()).select_from(TaxForm)) == 2


def test_multi_payer_consolidated_keeps_each_payer(session: Session) -> None:
    uid = _user(session)
    result = _import(session, "tax_1099_consolidated", "two_payers_one_file", uid)
    assert (result.status, result.inserted_forms) == ("parsed", 2)
    rows = session.execute(
        select(TaxForm.payer_name, TaxForm.payer_tin_last4, TaxForm.account_last4).order_by(
            TaxForm.id
        )
    ).all()
    assert [tuple(r) for r in rows] == [
        ("Alder Brokerage Group", "0031", "2222"),
        ("Birchwood Securities", "0032", "4444"),
    ]


def test_consolidated_with_unreadable_amounts_inserts_no_empty_form(session: Session) -> None:
    result = _import(session, "tax_1099_consolidated", "no_amounts", _user(session))
    assert result.status == "error" and result.inserted_forms == 0
    assert session.scalar(select(func.count()).select_from(TaxForm)) == 0


def test_w2_identical_without_ein_is_not_collapsed(session: Session) -> None:
    result = _import(session, "tax_w2", "no_ein_identical", _user(session))
    assert (result.status, result.form_types, result.inserted_forms) == (
        "parsed",
        ("w2", "w2"),
        2,
    )
    assert any("Two identical W-2 forms" in w for w in result.warnings)


def test_partial_success_keeps_the_readable_form_and_warns(session: Session) -> None:
    uid = _user(session)
    result = _import(session, "tax_1099_int", "second_form_blank_box1", uid)
    assert result.status == "parsed" and result.error is None
    assert result.form_types == ("1099-int",) and result.inserted_forms == 1
    assert any("skipped" in w and "box 1" in w for w in result.warnings)
    doc = session.get(TaxDocument, result.document_id)
    assert doc is not None and doc.status == "parsed"
    assert doc.warnings is not None and "skipped" in doc.warnings


def test_file_with_nothing_readable_is_still_an_error(session: Session) -> None:
    uid = _user(session)
    result = _import(session, "tax_w2", "no_box1", uid)
    assert result.status == "error" and result.inserted_forms == 0
    assert result.error is not None and not re.search(r"\d{6}", result.error)


def test_mixed_package_records_one_form_per_parser_and_multi_parser_id(session: Session) -> None:
    uid = _user(session)
    result = _import(session, "tax_mixed_package", "w2_int_div", uid)
    assert result.status == "parsed" and result.inserted_forms == 3
    doc = session.get(TaxDocument, result.document_id)
    assert doc is not None and doc.parser_id == "multi"


def test_broker_statement_with_two_up_forms_imports_as_one_consolidated_form(
    session: Session,
) -> None:
    uid = _user(session)
    result = _import(session, "tax_1099_broker", "two_up", uid)
    assert result.status == "parsed" and result.form_types == ("1099-consolidated",)
    assert result.income_item_count == 13 and result.tax_item_count == 4
    form = session.scalars(select(TaxForm)).one()
    assert (form.payer_name, form.payer_tin_last4, form.account_last4) == (
        "Copperfield Securities",
        "0004",
        "7731",
    )


def test_identical_weak_named_forms_in_one_file_stay_and_reimport_dedupes(
    session: Session, tmp_path: Path
) -> None:
    uid = _user(session)
    src = generated_pdf("tax_w2", "no_ein_identical")
    first = import_tax_document(session, src, user_id=uid, original_name="w2_a.pdf")
    other = _redelivered(src, tmp_path, "w2_b.pdf")
    second = import_tax_document(session, other, user_id=uid, original_name="w2_b.pdf")
    assert (first.inserted_forms, second.inserted_forms) == (2, 0)
    assert second.skipped_duplicate_forms == 2
    assert session.scalar(select(func.count()).select_from(TaxForm)) == 2
