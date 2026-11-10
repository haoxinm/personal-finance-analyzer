"""Tax tables: migration, CHECK/UNIQUE constraints, cascade delete."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from personal_finance_analyzer.db.migrate import upgrade_head
from personal_finance_analyzer.db.models import (
    Base,
    IncomeItem,
    TaxDocument,
    TaxForm,
    TaxItem,
    User,
)
from personal_finance_analyzer.db.session import make_engine_for_path

_TABLES = ("tax_documents", "tax_forms", "income_items", "tax_items")


def _seed(session: Session, *, sha: str = "a" * 64) -> tuple[User, TaxDocument, TaxForm]:
    user = session.scalar(select(User).limit(1)) or User(name="Alex")
    session.add(user)
    session.flush()
    doc = TaxDocument(
        user_id=user.id, file_sha256=sha, original_name="w2.pdf", status="parsed", tax_year=2024
    )
    session.add(doc)
    session.flush()
    form = TaxForm(
        document_id=doc.id,
        user_id=user.id,
        tax_year=2024,
        form_type="w2",
        role="source",
        fingerprint="fp-" + sha[:4],
    )
    session.add(form)
    session.flush()
    return user, doc, form


def _income(form: TaxForm, **kw: object) -> IncomeItem:
    base: dict[str, object] = {
        "form_id": form.id,
        "user_id": form.user_id,
        "tax_year": 2024,
        "category": "salary_wages",
        "amount_minor": 100,
        "source_form": "W-2",
    }
    base.update(kw)
    return IncomeItem(**base)


def test_fresh_upgrade_creates_tax_tables(engine: Engine) -> None:
    assert set(_TABLES) <= set(inspect(engine).get_table_names())


def test_legacy_upgrade_path_recreates_tables(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    url = f"sqlite:///{db}"
    upgrade_head(url)
    eng = make_engine_for_path(db)
    with eng.begin() as conn:
        for name in reversed(_TABLES):
            conn.execute(text(f"DROP TABLE {name}"))
        conn.execute(text("UPDATE alembic_version SET version_num='0003_statement_selected_pages'"))
    eng.dispose()
    upgrade_head(url)
    eng = make_engine_for_path(db)
    insp = inspect(eng)
    assert set(_TABLES) <= set(insp.get_table_names())
    for name in _TABLES:
        expected = {ix.name for ix in Base.metadata.tables[name].indexes}
        assert {ix["name"] for ix in insp.get_indexes(name)} >= expected
    eng.dispose()


def test_bad_income_category_rejected(session: Session) -> None:
    _, _, form = _seed(session)
    session.add(_income(form, category="lottery"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_negative_income_only_allowed_for_capital_gains(session: Session) -> None:
    _, _, form = _seed(session)
    session.add(_income(form, category="long_term_capital_gain", amount_minor=-500))
    session.flush()
    session.add(_income(form, category="salary_wages", amount_minor=-1))
    with pytest.raises(IntegrityError):
        session.flush()


def test_taxitem_category_and_sign_checks(session: Session) -> None:
    _, _, form = _seed(session)

    def item(**kw: object) -> TaxItem:
        base: dict[str, object] = {
            "form_id": form.id,
            "user_id": form.user_id,
            "tax_year": 2024,
            "category": "refund",
            "amount_minor": 10,
            "source_form": "1040",
        }
        base.update(kw)
        return TaxItem(**base)

    session.add(item())
    session.flush()
    session.add(item(amount_minor=-1))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    _, _, form = _seed(session, sha="b" * 64)
    session.add(item(category="bogus", form_id=form.id, user_id=form.user_id))
    with pytest.raises(IntegrityError):
        session.flush()


def test_payer_tin_last4_must_be_four_chars(session: Session) -> None:
    user, doc, _ = _seed(session)
    session.add(
        TaxForm(
            document_id=doc.id,
            user_id=user.id,
            tax_year=2024,
            form_type="w2",
            role="source",
            fingerprint="other",
            payer_tin_last4="123456789",
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_bad_form_type_and_role_rejected(session: Session) -> None:
    user, doc, _ = _seed(session)
    for kw in ({"form_type": "1099-zzz"}, {"role": "other"}):
        vals: dict[str, object] = {
            "document_id": doc.id,
            "user_id": user.id,
            "tax_year": 2024,
            "form_type": "w2",
            "role": "source",
            "fingerprint": "x" + str(kw),
        }
        vals.update(kw)
        session.add(TaxForm(**vals))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
        user, doc, _ = _seed(session)


def test_fingerprint_unique_per_user_year(session: Session) -> None:
    user, doc, form = _seed(session)
    session.add(
        TaxForm(
            document_id=doc.id,
            user_id=user.id,
            tax_year=2024,
            form_type="w2",
            role="source",
            fingerprint=form.fingerprint,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_deleting_document_cascades_to_forms_and_items(session: Session) -> None:
    _, doc, form = _seed(session)
    session.add(_income(form))
    session.add(
        TaxItem(
            form_id=form.id,
            user_id=form.user_id,
            tax_year=2024,
            category="medicare_tax",
            amount_minor=5,
            source_form="W-2",
        )
    )
    session.commit()
    session.delete(doc)
    session.commit()
    session.expire_all()
    for model in (TaxForm, IncomeItem, TaxItem):
        assert session.scalars(select(model)).all() == []


def test_document_sha_unique_per_user_not_global(session: Session) -> None:
    user, doc, _ = _seed(session)
    other = User(name="Sam")
    session.add(other)
    session.flush()
    # another user may hold the same PDF ...
    session.add(
        TaxDocument(
            user_id=other.id, file_sha256=doc.file_sha256, original_name="w2.pdf", status="parsed"
        )
    )
    session.flush()
    # ... but the same user may not hold it twice
    session.add(
        TaxDocument(
            user_id=user.id, file_sha256=doc.file_sha256, original_name="w2.pdf", status="parsed"
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
