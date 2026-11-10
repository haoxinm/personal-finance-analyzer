"""Seeding helpers for tax tests that need rows rather than PDFs (profiles, joint returns, corrected
forms). Parsed PDFs go through `tests/ingest/tax_support.py` and the real import pipeline."""

from __future__ import annotations

from itertools import count

from sqlalchemy.orm import Session

from personal_finance_analyzer.core.tax_types import FORM_ROLE
from personal_finance_analyzer.db.models import (
    IncomeItem,
    TaxDocument,
    TaxForm,
    TaxItem,
    TaxProfile,
    User,
)

_seq = count(1)


def make_user(session: Session, name: str) -> int:
    user = User(name=name)
    session.add(user)
    session.flush()
    return user.id


def add_form(
    session: Session,
    user_id: int,
    year: int,
    form_type: str,
    *,
    income: dict[str, int] | None = None,
    taxes: dict[str, int] | None = None,
    payer: str | None = None,
    tin: str | None = None,
    account: str | None = None,
    corrected: bool = False,
    filing_status: str | None = None,
    income_boxes: dict[str, str] | None = None,
) -> TaxForm:
    """One form with its items. Income categories that are memo categories are stored as such."""
    n = next(_seq)
    doc = TaxDocument(
        user_id=user_id,
        file_sha256=f"sha{n}",
        original_name=f"f{n}.pdf",
        tax_year=year,
        status="parsed",
    )
    session.add(doc)
    session.flush()
    form = TaxForm(
        document_id=doc.id,
        user_id=user_id,
        tax_year=year,
        form_type=form_type,
        role=FORM_ROLE[form_type],  # type: ignore[index]
        payer_name=payer,
        payer_tin_last4=tin,
        account_last4=account,
        is_corrected=corrected,
        filing_status=filing_status,
        fingerprint=f"fp{n}",
    )
    session.add(form)
    session.flush()
    for category, amount in (income or {}).items():
        session.add(
            IncomeItem(
                form_id=form.id,
                user_id=user_id,
                tax_year=year,
                category=category,
                amount_minor=amount,
                source_form=form_type.upper(),
                box=(income_boxes or {}).get(category),
                is_memo=category in ("retirement_deferral", "hsa_employer", "qualified_dividends"),
            )
        )
    for category, amount in (taxes or {}).items():
        session.add(
            TaxItem(
                form_id=form.id,
                user_id=user_id,
                tax_year=year,
                category=category,
                amount_minor=amount,
                source_form=form_type.upper(),
            )
        )
    session.flush()
    return form


def add_profile(
    session: Session, user_id: int, year: int, filing_status: str = "single", **fields: object
) -> TaxProfile:
    row = TaxProfile(user_id=user_id, tax_year=year, filing_status=filing_status, **fields)
    session.add(row)
    session.flush()
    return row
