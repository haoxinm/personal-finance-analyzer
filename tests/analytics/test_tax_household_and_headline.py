"""Household double counting, netted refund/owed, the backend headline tax, and corrected forms that
replace their originals (`analytics/tax_query.py`)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from spend_analyzer.analytics.tax_query import (
    TaxQuery,
    run_income_by_payer,
    run_tax_summary,
    source_figures,
)
from tests.analytics.tax_seed import add_form, add_profile, make_user

D = 100
Y = 2024


def _all(session: Session, **kw: object):  # type: ignore[no-untyped-def]
    (summary,) = run_tax_summary(session, TaxQuery(**kw))  # type: ignore[arg-type]
    return summary


# --- P0: a joint return is not counted twice -------------------------------------------------------


def _joint_household(session: Session, *, link: bool) -> tuple[int, int]:
    a, b = make_user(session, "a"), make_user(session, "b")
    add_form(
        session,
        a,
        Y,
        "w2",
        income={"salary_wages": 120_000 * D},
        taxes={"federal_income_tax_withheld": 15_000 * D, "medicare_tax": 1_740 * D},
        tin="0001",
    )
    add_form(
        session,
        b,
        Y,
        "w2",
        income={"salary_wages": 60_000 * D},
        taxes={"federal_income_tax_withheld": 5_000 * D, "medicare_tax": 870 * D},
        tin="0002",
    )
    # A's joint return already contains both W-2s
    add_form(
        session,
        a,
        Y,
        "1040",
        income={"salary_wages": 180_000 * D},
        taxes={"total_federal_tax": 20_000 * D, "total_payments": 20_000 * D, "refund": 100 * D},
        filing_status="mfj",
    )
    if link:
        add_profile(session, a, Y, "mfj", joint_with_user_id=b)
    return a, b


def test_linked_spouse_income_is_not_added_on_top_of_the_joint_return(session: Session) -> None:
    _joint_household(session, link=True)
    s = _all(session)
    assert s.total_income_minor == 180_000 * D  # the return's figure, not 240,000
    assert s.income_basis == "return"
    assert s.withheld["federal_income_tax_withheld"] == 20_000 * D  # both W-2s' withholding
    assert s.liability["total_federal_tax"] == 20_000 * D
    assert any("already included in the joint return" in w for w in s.warnings)


def test_without_a_link_the_documented_behaviour_is_unchanged(session: Session) -> None:
    _joint_household(session, link=False)
    assert _all(session).total_income_minor == 240_000 * D  # nothing says B is A's spouse


def test_link_on_either_spouses_profile_counts(session: Session) -> None:
    a, b = _joint_household(session, link=False)
    add_profile(session, b, Y, "mfj", joint_with_user_id=a)
    assert _all(session).total_income_minor == 180_000 * D


def test_the_same_joint_return_imported_for_both_spouses_is_counted_once(session: Session) -> None:
    _, b = _joint_household(session, link=True)
    add_form(
        session,
        b,
        Y,
        "1040",
        income={"salary_wages": 180_000 * D},
        taxes={"total_federal_tax": 20_000 * D, "total_payments": 20_000 * D, "refund": 100 * D},
        filing_status="mfj",
    )
    s = _all(session)
    assert s.total_income_minor == 180_000 * D
    assert s.liability["total_federal_tax"] == 20_000 * D
    assert s.payments_minor == 20_000 * D
    assert s.refund_minor == 100 * D


def test_a_link_without_any_return_just_sums_sources(session: Session) -> None:
    a, b = make_user(session, "a"), make_user(session, "b")
    add_form(session, a, Y, "w2", income={"salary_wages": 100 * D}, tin="0001")
    add_form(session, b, Y, "w2", income={"salary_wages": 50 * D}, tin="0002")
    add_profile(session, a, Y, "mfj", joint_with_user_id=b)
    s = _all(session)
    assert s.total_income_minor == 150 * D
    assert not any("already included" in w for w in s.warnings)


def test_per_user_rows_keep_each_users_figures_and_say_what_is_shared(session: Session) -> None:
    a, b = _joint_household(session, link=True)
    rows = {s.user_id: s for s in run_tax_summary(session, TaxQuery(per_user=True))}
    assert rows[a].total_income_minor == 180_000 * D  # the joint return
    assert rows[b].total_income_minor == 60_000 * D  # B's own W-2
    assert any("joint return: its income, tax and refund cover both" in w for w in rows[a].warnings)
    assert any("also included in the joint return imported for user" in w for w in rows[b].warnings)


# --- P0: refund and amount owed are netted ------------------------------------------------------------


def test_refund_and_owed_of_different_users_are_netted(session: Session) -> None:
    a, b = make_user(session, "a"), make_user(session, "b")
    add_form(
        session,
        a,
        Y,
        "1040",
        taxes={"total_federal_tax": 1, "refund": 1_000 * D},
        filing_status="single",
    )
    add_form(
        session,
        b,
        Y,
        "1040",
        taxes={"total_federal_tax": 1, "amount_owed": 400 * D},
        filing_status="single",
    )
    s = _all(session)
    assert (s.refund_minor, s.amount_owed_minor) == (600 * D, 0)
    # and the other way round
    add_form(
        session,
        b,
        2023,
        "1040",
        taxes={"total_federal_tax": 1, "amount_owed": 900 * D},
        filing_status="single",
    )
    add_form(
        session,
        a,
        2023,
        "1040",
        taxes={"total_federal_tax": 1, "refund": 200 * D},
        filing_status="single",
    )
    s23 = next(x for x in run_tax_summary(session, TaxQuery()) if x.tax_year == 2023)
    assert (s23.refund_minor, s23.amount_owed_minor) == (0, 700 * D)


def test_refund_and_owed_netting_keeps_none_when_no_return_has_either(session: Session) -> None:
    a = make_user(session, "a")
    add_form(session, a, Y, "w2", income={"salary_wages": 100 * D}, tin="0001")
    s = _all(session)
    assert s.refund_minor is None and s.amount_owed_minor is None


# --- P1: the headline tax -----------------------------------------------------------------------------


def test_headline_from_the_return_counts_additional_medicare_once(session: Session) -> None:
    a = make_user(session, "a")
    add_form(
        session,
        a,
        Y,
        "w2",
        income={"salary_wages": 300_000 * D},
        taxes={
            "federal_income_tax_withheld": 60_000 * D,
            "medicare_tax": 5_000 * D,
            "social_security_tax": 10_000 * D,
        },
        tin="0001",
    )
    add_form(
        session,
        a,
        Y,
        "1040",
        taxes={"total_federal_tax": 70_000 * D, "additional_medicare_tax": 900 * D},
        filing_status="single",
    )
    h = _all(session).headline
    assert h is not None
    assert h.federal_basis == "return"
    assert h.federal_minor == (70_000 - 900) * D  # 0.9% is already inside the W-2 Medicare tax
    assert h.fica_minor == 15_000 * D
    assert h.total_minor == h.federal_minor + h.fica_minor + h.state_minor


def test_headline_additional_medicare_is_capped_by_the_w2_medicare_tax(session: Session) -> None:
    a = make_user(session, "a")
    add_form(
        session,
        a,
        Y,
        "w2",
        income={"salary_wages": 1 * D},
        taxes={"medicare_tax": 100 * D},
        tin="0001",
    )
    add_form(
        session,
        a,
        Y,
        "1040",
        taxes={"total_federal_tax": 5_000 * D, "additional_medicare_tax": 900 * D},
        filing_status="single",
    )
    h = _all(session).headline
    assert h is not None and h.federal_minor == (5_000 - 100) * D


def test_headline_prefers_the_estimate_over_withholding_without_a_return(session: Session) -> None:
    a = make_user(session, "a")
    add_form(
        session,
        a,
        Y,
        "w2",
        income={"salary_wages": 80_000 * D},
        taxes={"federal_income_tax_withheld": 10_000 * D, "state_income_tax_withheld": 2_000 * D},
        tin="0001",
    )
    h = _all(session).headline
    assert h is not None
    assert h.federal_basis == "estimate"
    assert h.federal_minor == 9_447 * D  # not the 10,000 withheld
    assert h.state_minor == 2_000 * D


def test_headline_falls_back_to_withholding_without_parameters(session: Session) -> None:
    a = make_user(session, "a")
    add_form(
        session,
        a,
        2020,
        "w2",
        income={"salary_wages": 80_000 * D},
        taxes={"federal_income_tax_withheld": 10_000 * D},
        tin="0001",
    )
    h = _all(session).headline
    assert h is not None and h.federal_basis == "withholding" and h.federal_minor == 10_000 * D


def test_headline_is_absent_without_any_tax_figure(session: Session) -> None:
    a = make_user(session, "a")
    add_form(session, a, 2020, "1099-int", income={"interest": 10 * D}, tin="0001")
    assert _all(session).headline is None


def test_headline_mixed_across_users(session: Session) -> None:
    a, b = make_user(session, "a"), make_user(session, "b")
    add_form(
        session,
        a,
        Y,
        "w2",
        income={"salary_wages": 80_000 * D},
        taxes={"federal_income_tax_withheld": 10_000 * D},
        tin="0001",
    )
    add_form(session, b, Y, "1040", taxes={"total_federal_tax": 3_000 * D}, filing_status="single")
    h = _all(session).headline
    assert h is not None and h.federal_basis == "mixed"
    assert h.federal_minor == 9_447 * D + 3_000 * D


def test_headline_for_a_joint_household_uses_the_return_once(session: Session) -> None:
    _joint_household(session, link=True)
    h = _all(session).headline
    assert h is not None and h.federal_basis == "return"
    assert h.federal_minor == 20_000 * D  # B's withholding/estimate is not added to the return


# --- P1: corrected forms replace their originals ---------------------------------------------------------


def _w2(
    session: Session, user: int, wages: int, *, tin: str | None = "0001", corrected: bool = False
):  # type: ignore[no-untyped-def]
    return add_form(
        session,
        user,
        Y,
        "w2",
        income={"salary_wages": wages * D},
        taxes={"federal_income_tax_withheld": wages // 10 * D},
        tin=tin,
        corrected=corrected,
    )


def test_corrected_form_replaces_the_original(session: Session) -> None:
    a = make_user(session, "a")
    original = _w2(session, a, 50_000)
    corrected = _w2(session, a, 52_000, corrected=True)
    s = _all(session)
    assert s.total_income_minor == 52_000 * D
    assert s.withheld["federal_income_tax_withheld"] == 5_200 * D
    assert [(f.form_id, f.superseded_by_form_id) for f in s.superseded] == [
        (original.id, corrected.id)
    ]
    assert any("replaced by a corrected form" in w for w in s.warnings)
    assert not any("could not be matched" in w for w in s.warnings)
    assert s.form_count == 1


def test_import_order_does_not_matter(session: Session) -> None:
    a = make_user(session, "a")
    corrected = _w2(session, a, 52_000, corrected=True)
    original = _w2(session, a, 50_000)
    s = _all(session)
    assert s.total_income_minor == 52_000 * D
    assert [(f.form_id, f.superseded_by_form_id) for f in s.superseded] == [
        (original.id, corrected.id)
    ]


def test_a_second_correction_replaces_the_first(session: Session) -> None:
    a = make_user(session, "a")
    _w2(session, a, 50_000)
    c1 = _w2(session, a, 52_000, corrected=True)
    c2 = _w2(session, a, 53_000, corrected=True)
    s = _all(session)
    assert s.total_income_minor == 53_000 * D
    assert {f.superseded_by_form_id for f in s.superseded} == {c2.id}
    assert len(s.superseded) == 2 and c1.id in {f.form_id for f in s.superseded}


def test_matching_needs_user_year_type_tin_and_account(session: Session) -> None:
    a, b = make_user(session, "a"), make_user(session, "b")
    _w2(session, a, 50_000, tin="0001")
    _w2(session, a, 52_000, tin="0002", corrected=True)  # different employer
    _w2(session, b, 51_000, tin="0001", corrected=True)  # different user
    add_form(session, a, Y, "1099-int", income={"interest": 100 * D}, tin="0001", account="1111")
    add_form(
        session,
        a,
        Y,
        "1099-int",
        income={"interest": 120 * D},
        tin="0001",
        account="2222",
        corrected=True,
    )
    s = _all(session, user_ids=[a])
    assert s.superseded == []
    assert s.total_income_minor == (50_000 + 52_000 + 100 + 120) * D  # nothing replaced
    assert any("could not be matched" in w for w in s.warnings)  # the old warning stays


def test_same_account_1099_is_matched(session: Session) -> None:
    a = make_user(session, "a")
    add_form(session, a, Y, "1099-int", income={"interest": 100 * D}, tin="0001", account="1111")
    add_form(
        session,
        a,
        Y,
        "1099-int",
        income={"interest": 120 * D},
        tin="0001",
        account="1111",
        corrected=True,
    )
    s = _all(session)
    assert s.total_income_minor == 120 * D and len(s.superseded) == 1


def test_forms_without_any_identifier_are_never_matched(session: Session) -> None:
    a = make_user(session, "a")
    _w2(session, a, 50_000, tin=None)
    _w2(session, a, 52_000, tin=None, corrected=True)
    s = _all(session)
    assert s.superseded == [] and s.total_income_minor == 102_000 * D


def test_two_originals_make_the_match_ambiguous(session: Session) -> None:
    a = make_user(session, "a")
    _w2(session, a, 30_000)
    _w2(session, a, 20_000)
    _w2(session, a, 31_000, corrected=True)
    s = _all(session)
    assert s.superseded == []
    assert any("could not be matched" in w for w in s.warnings)


def test_superseded_forms_leave_the_estimate_and_the_payer_list(session: Session) -> None:
    a = make_user(session, "a")
    _w2(session, a, 50_000)
    _w2(session, a, 52_000, corrected=True)
    figs = source_figures(session, a, Y)
    assert figs.income["salary_wages"] == 52_000 * D
    assert figs.tax["federal_income_tax_withheld"] == 5_200 * D
    assert len(figs.superseded) == 1
    rows = run_income_by_payer(session, TaxQuery())
    assert [r.amount_minor for r in rows] == [52_000 * D]
