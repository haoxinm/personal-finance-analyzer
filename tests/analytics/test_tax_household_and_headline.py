"""Household double counting, netted refund/owed, the backend headline tax, and corrected forms that
replace their originals (`analytics/tax_query.py`)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from personal_finance_analyzer.analytics.tax_estimate import run_tax_estimate
from personal_finance_analyzer.analytics.tax_query import (
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
            "medicare_tax": 5_250 * D,  # 1.45% x 300,000 + the 0.9% on the 100,000 over 200,000
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
    assert h.fica_minor == 15_250 * D
    assert h.total_minor == h.federal_minor + h.fica_minor + h.state_minor


def _wages_and_return(session: Session, medicare_tax: int) -> None:
    a = make_user(session, "a")
    add_form(
        session,
        a,
        Y,
        "w2",
        income={"salary_wages": 300_000 * D},
        taxes={"medicare_tax": medicare_tax},
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


def test_headline_subtracts_nothing_when_no_additional_medicare_was_withheld(
    session: Session,
) -> None:
    # box 6 is exactly 1.45% of the wages: the employer withheld none of the 900 the return
    # reports, so it is not inside FICA and must stay in the federal figure
    _wages_and_return(session, 4_350 * D)
    h = _all(session).headline
    assert h is not None and h.federal_minor == 70_000 * D and h.fica_minor == 4_350 * D


def test_headline_subtracts_only_the_part_that_was_withheld(session: Session) -> None:
    _wages_and_return(session, (4_350 + 450) * D)  # 450 of the 900 was withheld
    h = _all(session).headline
    assert h is not None and h.federal_minor == (70_000 - 450) * D


def test_headline_estimate_counts_additional_medicare_withheld_once(session: Session) -> None:
    a = make_user(session, "a")
    add_form(
        session,
        a,
        Y,
        "w2",
        income={"salary_wages": 260_000 * D},
        taxes={
            "federal_income_tax_withheld": 50_000 * D,
            "medicare_tax": 4_310 * D,  # 1.45% x 260,000 + 0.9% x 60,000
        },
        tin="0001",
    )
    est = run_tax_estimate(session, a, Y)
    assert est.calc.additional_medicare_tax_minor == 540 * D
    h = _all(session).headline
    assert h is not None and h.federal_basis == "estimate"
    assert h.federal_minor == est.calc.total_tax_minor - 540 * D  # the 540 is already in FICA
    assert h.fica_minor == 4_310 * D


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


def test_several_corrections_use_import_order_and_say_so(session: Session) -> None:
    # No issue date is stored with a document, so the correction imported last wins, and the user
    # is told that this rests on import order rather than on a date.
    a = make_user(session, "a")
    _w2(session, a, 50_000)
    c1 = _w2(session, a, 52_000, corrected=True)
    c2 = _w2(session, a, 53_000, corrected=True)
    s = _all(session)
    assert s.total_income_minor == 53_000 * D
    flagged = {f.form_id: f.was_corrected for f in s.superseded}
    assert flagged[c1.id] is True and sum(flagged.values()) == 1  # only c1 was itself a correction
    chain = [w for w in s.warnings if "More than one corrected form" in w]
    assert len(chain) == 1 and f"#{c2.id}" in chain[0] and "No issue date" in chain[0]
    est = run_tax_estimate(session, a, Y)
    assert any("More than one corrected form" in w for w in est.warnings)


def test_a_single_correction_has_no_import_order_warning(session: Session) -> None:
    a = make_user(session, "a")
    _w2(session, a, 50_000)
    _w2(session, a, 52_000, corrected=True)
    s = _all(session)
    assert not any("More than one corrected form" in w for w in s.warnings)
    assert [f.was_corrected for f in s.superseded] == [False]


# --- S4: figures the estimate needs from the stored W-2s ----------------------------------------------


def _w2_full(session: Session, user: int, year: int, wages: int, *, tin: str, **taxes: int) -> None:
    add_form(
        session,
        user,
        year,
        "w2",
        income={"salary_wages": wages * D},
        taxes={k: v * D for k, v in taxes.items()},
        tin=tin,
    )


def test_two_employers_produce_the_excess_social_security_credit(session: Session) -> None:
    a = make_user(session, "a")
    _w2_full(
        session,
        a,
        2025,
        120_000,
        tin="1111",
        social_security_tax=7_440,
        federal_income_tax_withheld=15_000,
    )
    _w2_full(
        session,
        a,
        2025,
        120_000,
        tin="2222",
        social_security_tax=7_440,
        federal_income_tax_withheld=15_000,
    )
    figs = source_figures(session, a, 2025)
    assert figs.social_security_employers == 2
    est = run_tax_estimate(session, a, 2025)
    assert est.calc.excess_social_security_credit_minor == 396_180  # 14,880 - 10,918.20
    assert est.user_payments_minor == 30_000 * D + 396_180


def test_one_employer_is_not_an_excess_credit_even_with_two_w2s(session: Session) -> None:
    a = make_user(session, "a")
    _w2_full(session, a, 2025, 190_000, tin="1111", social_security_tax=11_780)
    assert source_figures(session, a, 2025).social_security_employers == 1
    assert run_tax_estimate(session, a, 2025).calc.excess_social_security_credit_minor == 0
    # a second W-2 with the same employer identifier is still one employer
    _w2_full(session, a, 2025, 10_000, tin="1111", social_security_tax=620)
    assert source_figures(session, a, 2025).social_security_employers == 1
    assert run_tax_estimate(session, a, 2025).calc.excess_social_security_credit_minor == 0


def test_spouses_each_with_one_employer_get_no_excess_credit(session: Session) -> None:
    a, b = make_user(session, "a"), make_user(session, "b")
    add_profile(session, a, 2025, "mfj", joint_with_user_id=b)
    _w2_full(session, a, 2025, 120_000, tin="1111", social_security_tax=7_440)
    _w2_full(session, b, 2025, 120_000, tin="2222", social_security_tax=7_440)
    est = run_tax_estimate(session, a, 2025)
    assert est.calc.excess_social_security_credit_minor == 0


def test_additional_medicare_from_stored_w2s_in_a_joint_household(session: Session) -> None:
    a, b = make_user(session, "a"), make_user(session, "b")
    add_profile(session, a, 2024, "mfj", joint_with_user_id=b)
    for user, tin in ((a, "1111"), (b, "2222")):
        _w2_full(
            session,
            user,
            2024,
            180_000,
            tin=tin,
            medicare_tax=2_610,
            federal_income_tax_withheld=30_000,
        )
    ea, eb = run_tax_estimate(session, a, 2024), run_tax_estimate(session, b, 2024)
    assert ea.calc.additional_medicare_tax_minor == 990 * D
    assert ea.calc.additional_medicare_withheld_minor == 0
    assert ea.user_additional_medicare_withheld_minor == 0
    assert ea.user_total_tax_minor + eb.user_total_tax_minor == ea.calc.total_tax_minor
    assert ea.user_result_minor + eb.user_result_minor == ea.calc.result_minor


def test_user_payments_include_refundable_credit_and_withheld_additional_medicare(
    session: Session,
) -> None:
    a = make_user(session, "a")
    add_profile(session, a, 2024, "single", refundable_credits_minor=1_000 * D)
    _w2_full(
        session,
        a,
        2024,
        260_000,
        tin="1111",
        medicare_tax=4_310,
        federal_income_tax_withheld=50_000,
    )
    est = run_tax_estimate(session, a, 2024)
    assert est.user_additional_medicare_withheld_minor == 540 * D
    assert est.user_payments_minor == (50_000 + 1_000 + 540) * D
    assert est.user_payments_minor == est.calc.total_payments_minor
    assert est.user_result_minor == est.calc.result_minor


def test_457b_deferrals_are_read_from_box_12_code_g(session: Session) -> None:
    a = make_user(session, "a")
    add_form(
        session,
        a,
        2024,
        "w2",
        income={"salary_wages": 100_000 * D, "retirement_deferral": 20_000 * D},
        tin="1111",
        income_boxes={"retirement_deferral": "12-G"},
    )
    figs = source_figures(session, a, 2024)
    assert figs.deferral_457b_minor == 20_000 * D
    assert figs.income["retirement_deferral"] == 20_000 * D
    other = make_user(session, "b")
    add_form(
        session,
        other,
        2024,
        "w2",
        income={"salary_wages": 1 * D, "retirement_deferral": 5 * D},
        tin="1111",
        income_boxes={"retirement_deferral": "12-D"},
    )
    assert source_figures(session, other, 2024).deferral_457b_minor == 0


def test_both_spouses_itemizing_is_summed_and_said_so(session: Session) -> None:
    a, b = make_user(session, "a"), make_user(session, "b")
    add_profile(
        session,
        a,
        2024,
        "mfj",
        joint_with_user_id=b,
        deduction_mode="itemized",
        itemized_deductions_minor=20_000 * D,
    )
    pb = add_profile(
        session,
        b,
        2024,
        "mfj",
        joint_with_user_id=a,
        deduction_mode="itemized",
        itemized_deductions_minor=15_000 * D,
    )
    _w2_full(session, a, 2024, 200_000, tin="1111")
    est = run_tax_estimate(session, a, 2024)
    assert est.calc.deduction_minor == 35_000 * D
    assert any("Both spouses entered itemized deductions" in w for w in est.warnings)
    # entering the household total once, on one profile, does not warn
    pb.itemized_deductions_minor = 0
    session.flush()
    again = run_tax_estimate(session, a, 2024)
    assert again.calc.deduction_minor == 20_000 * D
    assert not any("Both spouses entered itemized" in w for w in again.warnings)


def test_a_profile_counts_only_its_own_user_so_spouses_are_not_double_counted(
    session: Session,
) -> None:
    a, b = make_user(session, "a"), make_user(session, "b")
    # each spouse entered the household's boxes (4, both 65+): only 2 boxes and one senior per
    # profile are used, so the joint return gets 4 boxes and 2 seniors at most, not 8 and 4
    add_profile(
        session, a, 2025, "mfj", joint_with_user_id=b, age65_or_blind_count=4, age65_count=2
    )
    add_profile(
        session, b, 2025, "mfj", joint_with_user_id=a, age65_or_blind_count=4, age65_count=2
    )
    _w2_full(session, a, 2025, 100_000, tin="1111")
    est = run_tax_estimate(session, a, 2025)
    assert est.calc.senior_deduction_minor == 12_000 * D
    assert est.calc.deduction_minor == (31_500 + 4 * 1_600) * D
    assert not any("at most 4 apply" in w for w in est.warnings)
    assert any(
        "more age 65 / blind boxes than allowed" in w and "own boxes" in w for w in est.warnings
    )


def test_senior_deduction_for_a_household_sums_both_profiles(session: Session) -> None:
    a, b = make_user(session, "a"), make_user(session, "b")
    add_profile(
        session, a, 2025, "mfj", joint_with_user_id=b, age65_or_blind_count=1, age65_count=1
    )
    add_profile(
        session, b, 2025, "mfj", joint_with_user_id=a, age65_or_blind_count=1, age65_count=1
    )
    _w2_full(session, a, 2025, 100_000, tin="1111")
    est = run_tax_estimate(session, a, 2025)
    assert est.calc.senior_deduction_minor == 12_000 * D
    assert est.calc.deduction_minor == (31_500 + 2 * 1_600) * D
    assert est.calc.taxable_income_minor == (100_000 - 34_700 - 12_000) * D
