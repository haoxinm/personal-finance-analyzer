"""Manual edits of imported tax data: create a document without a file, add / change / delete
forms and their income and tax items, confirm reviewed figures.

Pure database code (no LLM, no network). Every function validates its input, commits on success
and rolls back on failure. Errors never echo the value that was rejected (it may be a
taxpayer-identifier-shaped string).

Provenance: a created form has ``extraction_method="manual"``; any change to a form sets
``is_user_edited``; a created or changed item gets ``origin="manual"``. Edits to a parsed or AI
form keep the imported fingerprint on purpose: delivering the original file again is still
recognised as a duplicate and cannot re-add the pre-edit figures. Manual forms get a random
fingerprint, so they never collide with an import.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal, get_args

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from personal_finance_analyzer.core.tax_types import (
    FORM_ROLE,
    INCOME_CATEGORIES,
    MEMO_INCOME_CATEGORIES,
    SIGNED_INCOME_CATEGORIES,
    TAX_CATEGORIES,
    FormType,
)
from personal_finance_analyzer.db.models import (
    IncomeItem,
    TaxDocument,
    TaxForm,
    TaxItem,
    User,
    utcnow_iso,
)
from personal_finance_analyzer.ingest.tax.common import (
    MAX_TAX_YEAR,
    MIN_TAX_YEAR,
    sanitize_payer_name,
)
from personal_finance_analyzer.ingest.tax.parsers._boxes import clean_jurisdiction
from personal_finance_analyzer.ingest.tax.pipeline import fingerprint_from_rows
from personal_finance_analyzer.ingest.tax.tin import scrub_display_name

MANUAL_WARNING: Final = "Figures on this document were entered manually."
DEFAULT_MANUAL_NAME: Final = "Manual entry"
FILING_STATUSES: Final = ("single", "mfj", "mfs", "hoh", "qss")
MAX_AMOUNT: Final = 10**12
MAX_LABEL_LEN: Final = 80
MAX_BOX_LEN: Final = 8
MAX_SOURCE_FORM_LEN: Final = 20
MAX_NAME_LEN: Final = 120

#: `source_form` printed on items of a manually entered form (as the parsers print it).
SOURCE_FORM_LABEL: Final[Mapping[str, str]] = {
    "w2": "W-2",
    "1099-int": "1099-INT",
    "1099-div": "1099-DIV",
    "1099-b": "1099-B",
    "1099-nec": "1099-NEC",
    "1099-misc": "1099-MISC",
    "1099-consolidated": "1099",
    "1040": "1040",
    "state-return": "State return",
}

_LAST4 = re.compile(r"[0-9]{4}")
_WS = re.compile(r"\s+")
_FORM_TYPES: Final[tuple[str, ...]] = get_args(FormType)


class EditError(Exception):
    """Base class; `status` is the HTTP status the API maps it to."""

    status = 400


class NotFound(EditError):
    status = 404


class Invalid(EditError):
    """The input failed validation (422)."""

    status = 422


class YearMismatch(EditError):
    """The form's tax year differs from the document's other forms (400)."""

    status = 400


class Conflict(EditError):
    status = 409


@dataclass(frozen=True, slots=True)
class IncomeItemData:
    category: str
    amount_minor: int
    box: str | None = None
    label: str | None = None
    is_memo: bool = False
    source_form: str | None = None


@dataclass(frozen=True, slots=True)
class TaxItemData:
    category: str
    amount_minor: int
    box: str | None = None
    label: str | None = None
    jurisdiction: str | None = None
    is_estimate: bool = False
    source_form: str | None = None


@dataclass(frozen=True, slots=True)
class FormData:
    form_type: str
    tax_year: int | None = None
    payer_name: str | None = None
    payer_tin_last4: str | None = None
    account_last4: str | None = None
    is_corrected: bool = False
    filing_status: str | None = None
    agi_minor: int | None = None
    taxable_income_minor: int | None = None
    income_items: tuple[IncomeItemData, ...] = field(default=())
    tax_items: tuple[TaxItemData, ...] = field(default=())


# --- validation ---------------------------------------------------------------------------------


def _year(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Invalid("tax_year must be an integer")
    if not MIN_TAX_YEAR <= value <= MAX_TAX_YEAR:
        raise Invalid(f"tax_year must be between {MIN_TAX_YEAR} and {MAX_TAX_YEAR}")
    return value


def _amount(value: object, name: str, *, minimum: int = -MAX_AMOUNT) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Invalid(f"{name} must be an integer number of cents")
    if abs(value) > MAX_AMOUNT or value < minimum:
        raise Invalid(f"{name} is out of range")
    return value


def _optional_amount(value: object, name: str, *, minimum: int = -MAX_AMOUNT) -> int | None:
    return None if value is None else _amount(value, name, minimum=minimum)


def _form_type(value: object) -> str:
    if not isinstance(value, str) or value not in _FORM_TYPES:
        raise Invalid("unknown form_type")
    return value


def _last4(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _LAST4.fullmatch(value):
        raise Invalid(f"{name} must be exactly four digits")
    return value


def _payer_name(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise Invalid("payer_name must be text")
    if not _WS.sub(" ", value).strip():
        return None
    cleaned = sanitize_payer_name(value)
    if cleaned is None:
        raise Invalid("payer_name must be a name, not an identifier number")
    return cleaned


def _filing_status(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in FILING_STATUSES:
        raise Invalid("unknown filing_status")
    return value


def _flag(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise Invalid(f"{name} must be true or false")
    return value


def _short_text(value: object, name: str, limit: int) -> str | None:
    """Scrubbed display text: whitespace collapsed, identifier shapes masked, `limit` chars."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise Invalid(f"{name} must be text")
    collapsed = _WS.sub(" ", value).strip()
    if not collapsed:
        return None
    if len(collapsed) > limit:
        raise Invalid(f"{name} is longer than {limit} characters")
    return scrub_display_name(collapsed)[:limit]


def _jurisdiction(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise Invalid("jurisdiction must be text")
    return clean_jurisdiction(value)


def _income_category(value: object) -> str:
    if not isinstance(value, str) or value not in INCOME_CATEGORIES:
        raise Invalid("unknown income category")
    return value


def _tax_category(value: object) -> str:
    if not isinstance(value, str) or value not in TAX_CATEGORIES:
        raise Invalid("unknown tax category")
    return value


def _check_income_sign(category: str, amount: int) -> None:
    if amount < 0 and category not in SIGNED_INCOME_CATEGORIES:
        raise Invalid("this income category cannot be negative")


# --- loading ------------------------------------------------------------------------------------


def _document(session: Session, document_id: int) -> TaxDocument:
    doc = session.get(TaxDocument, document_id)
    if doc is None:
        raise NotFound("tax document not found")
    return doc


def _form(session: Session, form_id: int) -> TaxForm:
    form = session.get(TaxForm, form_id)
    if form is None:
        raise NotFound("tax form not found")
    return form


def _commit(session: Session) -> None:
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise Conflict("the change conflicts with an existing tax form") from None


def _other_year_forms(
    session: Session, document_id: int, year: int, *, exclude: int | None
) -> bool:
    """True when the document holds a form (other than `exclude`) for a year other than `year`."""
    stmt = select(TaxForm.id).where(TaxForm.document_id == document_id, TaxForm.tax_year != year)
    if exclude is not None:
        stmt = stmt.where(TaxForm.id != exclude)
    return session.scalar(stmt.limit(1)) is not None


def _doc_warnings(doc: TaxDocument) -> list[str]:
    if not doc.warnings:
        return []
    loaded = json.loads(doc.warnings)
    return [str(w) for w in loaded] if isinstance(loaded, list) else []


# --- ownership ----------------------------------------------------------------------------------

OwnerKind = Literal["document", "form", "income_item", "tax_item"]
_OWNED_MODEL: Final[
    Mapping[str, type[TaxDocument] | type[TaxForm] | type[IncomeItem] | type[TaxItem]]
] = {
    "document": TaxDocument,
    "form": TaxForm,
    "income_item": IncomeItem,
    "tax_item": TaxItem,
}
_NOT_FOUND_MESSAGE: Final[Mapping[str, str]] = {
    "document": "tax document not found",
    "form": "tax form not found",
    "income_item": "income item not found",
    "tax_item": "tax item not found",
}


def require_owner(session: Session, kind: OwnerKind, target_id: int, user_id: int | None) -> None:
    """Raise `NotFound` unless the document / form / item belongs to `user_id`.

    ``user_id=None`` skips the check (CLI and "all users" callers). A mismatch is reported exactly
    like a missing row, so the response never reveals that the id exists for someone else.
    """
    if user_id is None:
        return
    model = _OWNED_MODEL[kind]
    owner = session.scalar(select(model.user_id).where(model.id == target_id))
    if owner != user_id:
        raise NotFound(_NOT_FOUND_MESSAGE[kind])


# --- documents ----------------------------------------------------------------------------------


def create_manual_document(
    session: Session, user_id: int, tax_year: int, name: str | None = None
) -> TaxDocument:
    """A document with no file, to hold figures typed in by the user."""
    year = _year(tax_year)
    if session.get(User, user_id) is None:
        raise NotFound("user not found")
    display = _short_text(name, "original_name", 200) or DEFAULT_MANUAL_NAME
    doc = TaxDocument(
        user_id=user_id,
        file_sha256="manual:" + uuid.uuid4().hex,
        original_name=display,
        stored_path=None,
        page_count=None,
        tax_year=year,
        status="parsed",
        parser_id="manual",
        parser_version=None,
        detect_score=None,
        warnings=None,
        error_detail=None,
    )
    session.add(doc)
    _commit(session)
    return doc


def _fingerprints_for_user(doc: TaxDocument, user_id: int) -> dict[int, str]:
    """``{form id: fingerprint}`` of `doc`'s forms as an import for `user_id` would store them.

    Manual forms keep their random fingerprint (it carries no owner). The position in the file
    (stored order) and the count of identical earlier forms re-create the import's ordinal and
    repeat numbering."""
    result: dict[int, str] = {}
    seen: Counter[tuple[str, ...]] = Counter()
    for ordinal, form in enumerate(sorted(doc.forms, key=lambda f: f.id)):
        if form.extraction_method == "manual":
            result[form.id] = form.fingerprint
            continue
        content = fingerprint_from_rows(user_id, form, file_sha256="", ordinal=-1)
        repeat = seen[(content,)]
        seen[(content,)] += 1
        result[form.id] = fingerprint_from_rows(
            user_id, form, file_sha256=doc.file_sha256, ordinal=ordinal, repeat=repeat
        )
    return result


def reassign_document(session: Session, document_id: int, user_id: int) -> TaxDocument:
    """Move a document, its forms and their items to another user in one transaction.

    Ownership is not a figure edit: nothing is marked user-edited. Raises `NotFound` for an
    unknown user and `Conflict` (nothing changed) when the target user already holds the same file
    or a form with the same (tax year, fingerprint).
    """
    doc = _document(session, document_id)
    target = session.get(User, user_id)
    if target is None:
        raise NotFound("user not found")
    if doc.user_id == user_id:
        return doc
    old = session.get(User, doc.user_id)
    if session.scalar(
        select(TaxDocument.id).where(
            TaxDocument.user_id == user_id, TaxDocument.file_sha256 == doc.file_sha256
        )
    ):
        raise Conflict(f"{target.name} already has this document")
    new_fingerprints = _fingerprints_for_user(doc, user_id)
    for form in doc.forms:
        new_fp = new_fingerprints[form.id]
        clash = session.scalar(
            select(TaxForm.id).where(
                TaxForm.user_id == user_id,
                TaxForm.tax_year == form.tax_year,
                TaxForm.fingerprint == new_fp,
            )
        )
        if clash is not None:
            label = SOURCE_FORM_LABEL.get(form.form_type, form.form_type)
            raise Conflict(
                f"{target.name} already has a {label} form for {form.tax_year} "
                "that matches this one"
            )
    keys = [(f.tax_year, new_fingerprints[f.id]) for f in doc.forms]
    if len(set(keys)) != len(keys):
        raise Conflict("two forms in this document would become identical for the new owner")
    form_ids = [f.id for f in doc.forms]
    doc.user_id = user_id
    session.execute(update(TaxForm).where(TaxForm.document_id == doc.id).values(user_id=user_id))
    for form in doc.forms:
        form.fingerprint = new_fingerprints[form.id]
    if form_ids:
        for model in (IncomeItem, TaxItem):
            session.execute(
                update(model).where(model.form_id.in_(form_ids)).values(user_id=user_id)
            )
    if old is not None:
        warnings = _doc_warnings(doc)
        warnings.append(f"Moved from {old.name}.")
        doc.warnings = json.dumps(warnings)
    _commit(session)
    session.expire_all()
    return doc


# --- forms --------------------------------------------------------------------------------------


def _build_income(form: TaxForm, data: IncomeItemData) -> IncomeItem:
    category = _income_category(data.category)
    amount = _amount(data.amount_minor, "amount_minor")
    _check_income_sign(category, amount)
    return IncomeItem(
        form_id=form.id,
        user_id=form.user_id,
        tax_year=form.tax_year,
        category=category,
        amount_minor=amount,
        currency="USD",
        source_form=_short_text(data.source_form, "source_form", MAX_SOURCE_FORM_LEN)
        or SOURCE_FORM_LABEL[form.form_type],
        box=_short_text(data.box, "box", MAX_BOX_LEN),
        label=_short_text(data.label, "label", MAX_LABEL_LEN),
        is_memo=_flag(data.is_memo, "is_memo") or category in MEMO_INCOME_CATEGORIES,
        origin="manual",
    )


def _build_tax(form: TaxForm, data: TaxItemData) -> TaxItem:
    return TaxItem(
        form_id=form.id,
        user_id=form.user_id,
        tax_year=form.tax_year,
        category=_tax_category(data.category),
        amount_minor=_amount(data.amount_minor, "amount_minor", minimum=0),
        currency="USD",
        source_form=_short_text(data.source_form, "source_form", MAX_SOURCE_FORM_LEN)
        or SOURCE_FORM_LABEL[form.form_type],
        box=_short_text(data.box, "box", MAX_BOX_LEN),
        label=_short_text(data.label, "label", MAX_LABEL_LEN),
        jurisdiction=_jurisdiction(data.jurisdiction),
        is_estimate=_flag(data.is_estimate, "is_estimate"),
        origin="manual",
    )


def add_form(session: Session, document_id: int, data: FormData) -> TaxForm:
    """Add a manually entered form (with optional items) to a document.

    A document in status `error` / `unsupported` becomes `parsed` with the manual-entry warning.
    """
    doc = _document(session, document_id)
    form_type = _form_type(data.form_type)
    year = _year(data.tax_year if data.tax_year is not None else doc.tax_year)
    if _other_year_forms(session, doc.id, year, exclude=None):
        raise YearMismatch("the form's tax year differs from the document's other forms")
    form = TaxForm(
        document_id=doc.id,
        user_id=doc.user_id,
        tax_year=year,
        form_type=form_type,
        role=FORM_ROLE[form_type],  # type: ignore[index]
        payer_name=_payer_name(data.payer_name),
        payer_tin_last4=_last4(data.payer_tin_last4, "payer_tin_last4"),
        account_last4=_last4(data.account_last4, "account_last4"),
        is_corrected=_flag(data.is_corrected, "is_corrected"),
        filing_status=_filing_status(data.filing_status),
        agi_minor=_optional_amount(data.agi_minor, "agi_minor"),
        taxable_income_minor=_optional_amount(
            data.taxable_income_minor, "taxable_income_minor", minimum=0
        ),
        fingerprint=hashlib.sha256(("manual|" + uuid.uuid4().hex).encode()).hexdigest(),
        extraction_method="manual",
        is_user_edited=False,
        created_at=utcnow_iso(),
    )
    session.add(form)
    session.flush()
    for income in data.income_items:
        session.add(_build_income(form, income))
    for tax in data.tax_items:
        session.add(_build_tax(form, tax))
    doc.tax_year = year
    if doc.status != "parsed":
        doc.status = "parsed"
        doc.error_detail = None
        warnings = _doc_warnings(doc)
        if MANUAL_WARNING not in warnings:
            warnings.append(MANUAL_WARNING)
            doc.warnings = json.dumps(warnings)
    session.flush()
    _commit(session)
    return form


_FORM_PATCH_FIELDS: Final = frozenset(
    {
        "form_type",
        "tax_year",
        "payer_name",
        "payer_tin_last4",
        "account_last4",
        "is_corrected",
        "filing_status",
        "agi_minor",
        "taxable_income_minor",
    }
)


def update_form(session: Session, form_id: int, patch: Mapping[str, object]) -> TaxForm:
    """Apply the fields present in `patch` (an explicit ``None`` clears a nullable field)."""
    form = _form(session, form_id)
    unknown = set(patch) - _FORM_PATCH_FIELDS
    if unknown:
        raise Invalid("unknown field in patch")
    values: dict[str, object] = {}
    try:
        if "form_type" in patch:
            form_type = _form_type(patch["form_type"])
            values["form_type"] = form_type
            values["role"] = FORM_ROLE[form_type]  # type: ignore[index]
        if "tax_year" in patch:
            values["tax_year"] = _year(patch["tax_year"])
        if "payer_name" in patch:
            values["payer_name"] = _payer_name(patch["payer_name"])
        if "payer_tin_last4" in patch:
            values["payer_tin_last4"] = _last4(patch["payer_tin_last4"], "payer_tin_last4")
        if "account_last4" in patch:
            values["account_last4"] = _last4(patch["account_last4"], "account_last4")
        if "is_corrected" in patch:
            values["is_corrected"] = _flag(patch["is_corrected"], "is_corrected")
        if "filing_status" in patch:
            values["filing_status"] = _filing_status(patch["filing_status"])
        if "agi_minor" in patch:
            values["agi_minor"] = _optional_amount(patch["agi_minor"], "agi_minor")
        if "taxable_income_minor" in patch:
            values["taxable_income_minor"] = _optional_amount(
                patch["taxable_income_minor"], "taxable_income_minor", minimum=0
            )
    except EditError:
        session.rollback()
        raise
    new_year = values.get("tax_year")
    if isinstance(new_year, int) and new_year != form.tax_year:
        # A document holds one tax year, so the year of one form moves the whole document: all
        # its forms and their items, in this one transaction (a fingerprint collision on any of
        # them rolls everything back with a 409). Forms already in some other year than this
        # form's cannot be reconciled by a single change.
        if _other_year_forms(session, form.document_id, form.tax_year, exclude=form.id):
            raise YearMismatch("the form's tax year differs from the document's other forms")
        try:
            _document(session, form.document_id).tax_year = new_year
            form_ids = select(TaxForm.id).where(TaxForm.document_id == form.document_id)
            session.execute(
                update(TaxForm)
                .where(TaxForm.document_id == form.document_id, TaxForm.id != form.id)
                .values(tax_year=new_year)
            )
            for model in (IncomeItem, TaxItem):
                session.execute(
                    update(model).where(model.form_id.in_(form_ids)).values(tax_year=new_year)
                )
        except IntegrityError:
            session.rollback()
            raise Conflict("the change conflicts with an existing tax form") from None
    for key, value in values.items():
        setattr(form, key, value)
    if patch:
        form.is_user_edited = True
    _commit(session)
    session.refresh(form)
    return form


def delete_form(session: Session, form_id: int) -> None:
    """Hard delete; the form's items go with it (cascade)."""
    form = _form(session, form_id)
    session.delete(form)
    _commit(session)


def confirm_form(session: Session, form_id: int) -> TaxForm:
    """The user reviewed this form: its AI-extracted items become `manual`."""
    form = _form(session, form_id)
    for model in (IncomeItem, TaxItem):
        session.execute(
            update(model)
            .where(model.form_id == form.id, model.origin == "llm")
            .values(origin="manual")
        )
    _commit(session)
    session.refresh(form)
    return form


# --- items --------------------------------------------------------------------------------------


def _touch(form: TaxForm) -> None:
    form.is_user_edited = True


def add_income_item(session: Session, form_id: int, data: IncomeItemData) -> IncomeItem:
    form = _form(session, form_id)
    item = _build_income(form, data)
    session.add(item)
    _touch(form)
    _commit(session)
    return item


def update_income_item(session: Session, item_id: int, patch: Mapping[str, object]) -> IncomeItem:
    item = session.get(IncomeItem, item_id)
    if item is None:
        raise NotFound("income item not found")
    allowed = {"category", "amount_minor", "box", "label", "is_memo", "source_form"}
    if set(patch) - allowed:
        raise Invalid("unknown field in patch")
    category = _income_category(patch["category"]) if "category" in patch else item.category
    amount = _amount(patch["amount_minor"], "amount_minor") if "amount_minor" in patch else None
    amount = item.amount_minor if amount is None else amount
    _check_income_sign(category, amount)
    box = _short_text(patch["box"], "box", MAX_BOX_LEN) if "box" in patch else item.box
    label = _short_text(patch["label"], "label", MAX_LABEL_LEN) if "label" in patch else item.label
    if "is_memo" in patch:
        is_memo = _flag(patch["is_memo"], "is_memo")
    elif category != item.category:
        is_memo = False  # re-derived below from the new category, never carried over
    else:
        is_memo = item.is_memo
    source = item.source_form
    if "source_form" in patch:
        source = (
            _short_text(patch["source_form"], "source_form", MAX_SOURCE_FORM_LEN)
            or (SOURCE_FORM_LABEL[item_form_type(session, item.form_id)])
        )
    item.category = category
    item.amount_minor = amount
    item.box = box
    item.label = label
    item.is_memo = is_memo or category in MEMO_INCOME_CATEGORIES
    item.source_form = source
    item.origin = "manual"
    _touch(_form(session, item.form_id))
    _commit(session)
    return item


def item_form_type(session: Session, form_id: int) -> str:
    return _form(session, form_id).form_type


def delete_income_item(session: Session, item_id: int) -> None:
    item = session.get(IncomeItem, item_id)
    if item is None:
        raise NotFound("income item not found")
    _touch(_form(session, item.form_id))
    session.delete(item)
    _commit(session)


def add_tax_item(session: Session, form_id: int, data: TaxItemData) -> TaxItem:
    form = _form(session, form_id)
    item = _build_tax(form, data)
    session.add(item)
    _touch(form)
    _commit(session)
    return item


def update_tax_item(session: Session, item_id: int, patch: Mapping[str, object]) -> TaxItem:
    item = session.get(TaxItem, item_id)
    if item is None:
        raise NotFound("tax item not found")
    allowed = {
        "category",
        "amount_minor",
        "box",
        "label",
        "jurisdiction",
        "is_estimate",
        "source_form",
    }
    if set(patch) - allowed:
        raise Invalid("unknown field in patch")
    category = _tax_category(patch["category"]) if "category" in patch else item.category
    amount = (
        _amount(patch["amount_minor"], "amount_minor", minimum=0)
        if "amount_minor" in patch
        else item.amount_minor
    )
    box = _short_text(patch["box"], "box", MAX_BOX_LEN) if "box" in patch else item.box
    label = _short_text(patch["label"], "label", MAX_LABEL_LEN) if "label" in patch else item.label
    jurisdiction = (
        _jurisdiction(patch["jurisdiction"]) if "jurisdiction" in patch else item.jurisdiction
    )
    estimate = (
        _flag(patch["is_estimate"], "is_estimate") if "is_estimate" in patch else item.is_estimate
    )
    source = item.source_form
    if "source_form" in patch:
        source = (
            _short_text(patch["source_form"], "source_form", MAX_SOURCE_FORM_LEN)
            or (SOURCE_FORM_LABEL[item_form_type(session, item.form_id)])
        )
    item.category = category
    item.amount_minor = amount
    item.box = box
    item.label = label
    item.jurisdiction = jurisdiction
    item.is_estimate = estimate
    item.source_form = source
    item.origin = "manual"
    _touch(_form(session, item.form_id))
    _commit(session)
    return item


def delete_tax_item(session: Session, item_id: int) -> None:
    item = session.get(TaxItem, item_id)
    if item is None:
        raise NotFound("tax item not found")
    _touch(_form(session, item.form_id))
    session.delete(item)
    _commit(session)
