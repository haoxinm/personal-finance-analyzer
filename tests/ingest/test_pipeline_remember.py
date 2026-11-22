"""A remembered extractor and re-proposing: what "Remember this extractor for this issuer" stores,
how a later proposal uses it, and `repropose` for statements that were uploaded before the choice
was made."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from personal_finance_analyzer.db.models import Issuer, LayoutSpec, Statement, User
from personal_finance_analyzer.ingest import pipeline
from tests.ingest.tax_support import generated_pdf
from tests.watch.support import add_layout_b_bank, add_layout_b_spec, statement_pdf

_FIRST = "normal"
_SECOND = "multiline"


@pytest.fixture
def user(session: Session) -> User:
    row = User(name="alice")
    session.add(row)
    session.flush()
    return row


def _propose(session: Session, user: User, variant: str) -> pipeline.ImportProposal:
    pdf = generated_pdf("layout_b_credit", variant)
    proposal = pipeline.propose_import(
        session, pdf, user_id=user.id, original_name=f"{variant}.pdf"
    )
    session.commit()
    return proposal


def _confirm(
    session: Session,
    proposal: pipeline.ImportProposal,
    *,
    issuer: Issuer,
    parser_id: str | None,
    layout_spec_id: int | None,
    remember: bool = True,
) -> pipeline.ImportResult:
    result = pipeline.confirm_import(
        session,
        proposal.statement_id,
        issuer_id=issuer.id,
        parser_id=parser_id,
        layout_spec_id=layout_spec_id,
        remember=remember,
    )
    session.commit()
    return result


def test_a_remembered_spec_wins_over_a_better_scoring_builtin(session: Session, user: User) -> None:
    issuer = add_layout_b_bank(session)
    spec = add_layout_b_spec(session, issuer, score=0.85)  # the built-in scores 0.95
    first = _propose(session, user, _FIRST)
    second = _propose(session, user, _SECOND)
    assert first.layout_spec_id is None and first.parser_id == "layout_b_credit"
    assert second.remembered is False

    result = _confirm(session, first, issuer=issuer, parser_id=None, layout_spec_id=spec.id)
    assert result.remembered is True
    session.refresh(issuer)
    assert (issuer.default_spec_id, issuer.default_parser_id) == (spec.id, None)

    again = pipeline.repropose(session, second.statement_id)
    assert (again.layout_spec_id, again.parser_id) == (spec.id, spec.parser_id)
    assert again.remembered is True and again.issuer_id == issuer.id
    assert again.confident is True  # the spec's own score (0.85) clears the threshold
    row = session.get(Statement, second.statement_id)
    assert row is not None and row.layout_spec_id == spec.id and row.parser_id == spec.parser_id


def test_a_remembered_builtin_wins_over_a_better_scoring_spec(session: Session, user: User) -> None:
    issuer = add_layout_b_bank(session)
    spec = add_layout_b_spec(session, issuer, score=0.99)  # beats the built-in (0.95)
    first = _propose(session, user, _FIRST)
    second = _propose(session, user, _SECOND)
    assert first.layout_spec_id == spec.id

    _confirm(session, first, issuer=issuer, parser_id="layout_b_credit", layout_spec_id=None)
    session.refresh(issuer)
    assert (issuer.default_spec_id, issuer.default_parser_id) == (None, "layout_b_credit")

    again = pipeline.repropose(session, second.statement_id)
    assert (again.parser_id, again.layout_spec_id) == ("layout_b_credit", None)
    assert again.remembered is True


def test_remembering_one_kind_clears_the_other(session: Session, user: User) -> None:
    issuer = add_layout_b_bank(session)
    spec = add_layout_b_spec(session, issuer, score=0.85)
    issuer.default_parser_id = "layout_b_credit"
    session.commit()
    first = _propose(session, user, _FIRST)
    _confirm(session, first, issuer=issuer, parser_id=None, layout_spec_id=spec.id)
    session.refresh(issuer)
    assert (issuer.default_spec_id, issuer.default_parser_id) == (spec.id, None)


def test_remember_is_not_blocked_by_a_reconciliation_mismatch_but_needs_rows(
    session: Session, user: User
) -> None:
    issuer = add_layout_b_bank(session)
    wrong = add_layout_b_spec(session, issuer, wrong=True, name="Wrong B")
    first = _propose(session, user, _FIRST)
    # A spec that finds no rows is not worth remembering.
    result = _confirm(session, first, issuer=issuer, parser_id=None, layout_spec_id=wrong.id)
    assert (result.parsed_count, result.remembered) == (0, False)
    session.refresh(issuer)
    assert issuer.default_spec_id is None and issuer.default_parser_id is None


def test_a_remembered_llm_is_proposed_but_never_confident(
    session: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    issuer = add_layout_b_bank(session)
    first = _propose(session, user, _FIRST)
    issuer.default_parser_id = "llm"
    session.commit()
    # Not configured: the remembered LLM cannot be used, normal scoring applies.
    assert pipeline.repropose(session, first.statement_id).parser_id == "layout_b_credit"

    monkeypatch.setenv("PFA_LLM__ENABLED", "true")
    monkeypatch.setenv("PFA_LLM__BASE_URL", "https://api.example.com")
    monkeypatch.setenv("PFA_LLM__MODEL", "m")
    # The built-in is confident for this layout: a remembered LLM does not override it.
    again = pipeline.repropose(session, first.statement_id)
    assert again.parser_id == "layout_b_credit" and again.remembered is False
    assert again.confident is True


def test_a_remembered_llm_is_proposed_when_no_local_extractor_is_confident(
    session: Session, user: User, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issuer = Issuer(name="Invented", slug="invented", match_terms='["Invented Card Co"]')
    session.add(issuer)
    session.commit()
    pdf = statement_pdf(tmp_path / "x.pdf")
    first = pipeline.propose_import(session, pdf, user_id=user.id, original_name="x.pdf")
    session.commit()
    issuer.default_parser_id = "llm"
    session.commit()
    monkeypatch.setenv("PFA_LLM__ENABLED", "true")
    monkeypatch.setenv("PFA_LLM__BASE_URL", "https://api.example.com")
    monkeypatch.setenv("PFA_LLM__MODEL", "m")
    again = pipeline.repropose(session, first.statement_id)
    assert again.parser_id == "llm" and again.remembered is True
    assert again.confident is False and again.detect_score is None


def test_a_remembered_extractor_that_is_not_confident_falls_back_to_scoring(
    session: Session, user: User
) -> None:
    issuer = add_layout_b_bank(session)
    spec = add_layout_b_spec(session, issuer, score=0.5)  # below the confident threshold
    first = _propose(session, user, _FIRST)
    assert first.layout_spec_id is None  # the built-in (0.95) beats it
    issuer.default_spec_id = spec.id
    session.commit()
    again = pipeline.repropose(session, first.statement_id)
    assert (again.parser_id, again.layout_spec_id, again.remembered) == (
        "layout_b_credit",
        None,
        False,
    )
    assert again.confident is True


def test_a_remembered_spec_resolves_to_its_newest_approved_revision(
    session: Session, user: User
) -> None:
    issuer = add_layout_b_bank(session)
    v1 = add_layout_b_spec(session, issuer, score=0.85)
    first = _propose(session, user, _FIRST)
    second = _propose(session, user, _SECOND)
    _confirm(session, first, issuer=issuer, parser_id=None, layout_spec_id=v1.id)
    v2 = LayoutSpec(
        name=v1.name,
        version=2,
        parser_id=f"{v1.parser_id[:-2]}v2",
        issuer_id=issuer.id,
        account_type=v1.account_type,
        spec_yaml=v1.spec_yaml,
        source="pasted",
        approved=False,
    )
    session.add(v2)
    session.commit()
    # An unapproved revision changes nothing.
    assert pipeline.repropose(session, second.statement_id).layout_spec_id == v1.id
    from personal_finance_analyzer.ingest.layout_spec import approve_spec

    approve_spec(session, v2)
    session.commit()
    session.refresh(issuer)
    assert issuer.default_spec_id == v2.id  # advanced when the revision was approved
    issuer.default_spec_id = v1.id  # (a pointer left on the old version still resolves to v2)
    session.commit()
    again = pipeline.repropose(session, second.statement_id)
    assert (again.layout_spec_id, again.remembered) == (v2.id, True)


def test_a_failed_extractor_is_not_proposed_again(session: Session, user: User) -> None:
    add_layout_b_bank(session)
    first = _propose(session, user, _FIRST)
    assert first.parser_id == "layout_b_credit"
    row = session.get(Statement, first.statement_id)
    assert row is not None
    row.status = pipeline.STATUS_UNSUPPORTED_LAYOUT
    session.commit()
    again = pipeline.repropose(session, first.statement_id)
    assert again.parser_id != "layout_b_credit" and again.confident is False
    row.status = pipeline.STATUS_AWAITING_EXTRACTOR
    session.commit()
    assert pipeline.repropose(session, first.statement_id).parser_id == "layout_b_credit"


def test_repropose_never_overwrites_a_statement_that_became_parsed(
    session: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_layout_b_bank(session)
    first = _propose(session, user, _FIRST)
    real = pipeline._propose_extractor

    def racing(*args: object, **kwargs: object) -> pipeline._Proposed:
        proposed = real(*args, **kwargs)  # type: ignore[arg-type]
        session.execute(
            update(Statement)
            .where(Statement.id == first.statement_id)
            .values(status=pipeline.STATUS_PARSED, parser_id="winner")
        )
        return proposed

    monkeypatch.setattr(pipeline, "_propose_extractor", racing)
    with pytest.raises(pipeline.PfaError, match="no longer pending"):
        pipeline.repropose(session, first.statement_id)
    session.expire_all()
    row = session.get(Statement, first.statement_id)
    assert row is not None and row.parser_id == "winner"


def test_a_remembered_spec_that_was_unapproved_falls_back_to_scoring(
    session: Session, user: User
) -> None:
    issuer = add_layout_b_bank(session)
    spec = add_layout_b_spec(session, issuer, score=0.85)
    first = _propose(session, user, _FIRST)
    second = _propose(session, user, _SECOND)
    _confirm(session, first, issuer=issuer, parser_id=None, layout_spec_id=spec.id)

    row = session.get(LayoutSpec, spec.id)
    assert row is not None
    row.approved = False
    session.commit()
    again = pipeline.repropose(session, second.statement_id)
    assert (again.parser_id, again.layout_spec_id, again.remembered) == (
        "layout_b_credit",
        None,
        False,
    )


def test_a_new_issuer_is_picked_up_by_a_later_proposal(session: Session, user: User) -> None:
    first = _propose(session, user, _FIRST)
    assert first.issuer_id is None
    issuer = add_layout_b_bank(session)
    again = pipeline.repropose(session, first.statement_id)
    assert again.issuer_id == issuer.id
    assert again.confident is True  # issuer matched and the built-in scores above 0.8


def test_dropping_a_pending_file_again_rescores_it(session: Session, user: User) -> None:
    issuer = add_layout_b_bank(session)
    spec = add_layout_b_spec(session, issuer, score=0.85)
    first = _propose(session, user, _FIRST)
    second = _propose(session, user, _SECOND)
    _confirm(session, first, issuer=issuer, parser_id=None, layout_spec_id=spec.id)

    pdf: Path = generated_pdf("layout_b_credit", _SECOND)
    redrop = pipeline.propose_import(session, pdf, user_id=user.id, original_name="again.pdf")
    assert redrop.statement_id == second.statement_id
    assert (redrop.layout_spec_id, redrop.remembered) == (spec.id, True)


def test_a_failed_statement_is_re_proposed_but_never_confident(
    session: Session, user: User
) -> None:
    issuer = add_layout_b_bank(session)
    first = _propose(session, user, _FIRST)
    row = session.get(Statement, first.statement_id)
    assert row is not None
    row.status = pipeline.STATUS_ERROR
    session.commit()
    redrop = _propose(session, user, _FIRST)
    assert redrop.status == "awaiting_extractor" and redrop.confident is False
    assert redrop.issuer_id == issuer.id


def test_repropose_refuses_a_parsed_statement(session: Session, user: User) -> None:
    issuer = add_layout_b_bank(session)
    first = _propose(session, user, _FIRST)
    _confirm(session, first, issuer=issuer, parser_id="layout_b_credit", layout_spec_id=None)
    with pytest.raises(pipeline.PfaError, match="cannot be re-proposed"):
        pipeline.repropose(session, first.statement_id)
