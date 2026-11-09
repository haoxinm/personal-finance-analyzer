"""Seed helpers shared by the net worth forecast tests (analytics and API)."""

from __future__ import annotations

import uuid
from datetime import date
from itertools import count

from sqlalchemy.orm import Session

from spend_analyzer.core.tax_types import FORM_ROLE
from spend_analyzer.db.models import (
    Account,
    BalanceEntry,
    IncomeItem,
    Issuer,
    Statement,
    TaxDocument,
    TaxForm,
    TaxItem,
    Transaction,
    User,
)

_seq = count(1)


def add_user(session: Session, name: str) -> int:
    user = User(name=name)
    session.add(user)
    session.flush()
    return user.id


def add_balance(
    session: Session, user_id: int, category: str, name: str, year: int, month: int, balance: int
) -> None:
    session.add(
        BalanceEntry(
            user_id=user_id,
            category=category,
            account_name=name,
            year=year,
            month=month,
            balance_minor=balance,
        )
    )
    session.flush()


def add_w2(
    session: Session,
    user_id: int,
    year: int,
    wages: int,
    taxes: dict[str, int],
    other_income: dict[str, int] | None = None,
) -> None:
    """A W-2 with `wages` in box 1 and the given tax items (all-in rate = sum(taxes) / total
    income). `other_income` adds non-wage lines (interest, dividends, ...) to the same form."""
    n = next(_seq)
    doc = TaxDocument(
        user_id=user_id,
        file_sha256=f"fc-sha{n}",
        original_name=f"w2-{n}.pdf",
        tax_year=year,
        status="parsed",
    )
    session.add(doc)
    session.flush()
    form = TaxForm(
        document_id=doc.id,
        user_id=user_id,
        tax_year=year,
        form_type="w2",
        role=FORM_ROLE["w2"],
        fingerprint=f"fc-fp{n}",
    )
    session.add(form)
    session.flush()
    session.add(
        IncomeItem(
            form_id=form.id,
            user_id=user_id,
            tax_year=year,
            category="salary_wages",
            amount_minor=wages,
            source_form="W2",
        )
    )
    for category, amount in (other_income or {}).items():
        session.add(
            IncomeItem(
                form_id=form.id,
                user_id=user_id,
                tax_year=year,
                category=category,
                amount_minor=amount,
                source_form="W2",
            )
        )
    for category, amount in taxes.items():
        session.add(
            TaxItem(
                form_id=form.id,
                user_id=user_id,
                tax_year=year,
                category=category,
                amount_minor=amount,
                source_form="W2",
            )
        )
    session.flush()


class SpendSeed:
    """One card account for a user; `purchase` inserts spend transactions on it."""

    def __init__(self, session: Session, user_id: int) -> None:
        self.session = session
        self.user_id = user_id
        suffix = uuid.uuid4().hex[:8]
        issuer = Issuer(name=f"Bank {suffix}", slug=f"bank-{suffix}")
        session.add(issuer)
        session.flush()
        self.account = Account(
            user_id=user_id, issuer_id=issuer.id, account_type="credit", display_name="Card"
        )
        session.add(self.account)
        session.flush()
        self.statement = Statement(
            file_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            original_name="s.pdf",
            status="parsed",
            account_id=self.account.id,
        )
        session.add(self.statement)
        session.flush()

    def purchase(self, posted: date, amount_minor: int, kind: str = "purchase") -> None:
        self.session.add(
            Transaction(
                statement_id=self.statement.id,
                account_id=self.account.id,
                user_id=self.user_id,
                posted_date=posted.isoformat(),
                description_raw="Shop",
                description_clean="Shop",
                merchant_key="shop",
                amount_minor=amount_minor,
                currency="USD",
                kind=kind,
                is_spend=kind in ("purchase", "fee", "interest"),
                dedupe_hash=uuid.uuid4().hex,
            )
        )
        self.session.flush()
