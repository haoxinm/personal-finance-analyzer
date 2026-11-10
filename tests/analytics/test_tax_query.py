"""Tax aggregation (`analytics/tax_query.py`): source-vs-return rule, memo items, rates."""

from __future__ import annotations

from itertools import count

import pytest
from sqlalchemy.orm import Session

from spend_analyzer.analytics.tax_query import (
    TaxQuery,
    get_tax_document,
    list_tax_documents,
    list_tax_years,
    run_income_by_payer,
    run_tax_summary,
)
from spend_analyzer.core.tax_types import FORM_ROLE
from spend_analyzer.db.models import IncomeItem, TaxDocument, TaxForm, TaxItem, User
from spend_analyzer.ingest.tax.tin import has_long_digit_run

_seq = count(1)


def _user(session: Session, name: str = "alice") -> int:
    user = User(name=name)
    session.add(user)
    session.flush()
    return user.id


def _form(
    session: Session,
    user_id: int,
    year: int,
    form_type: str,
    *,
    income: dict[str, int] | None = None,
    memo: dict[str, int] | None = None,
    taxes: dict[str, int] | None = None,
    payer: str | None = None,
    corrected: bool = False,
) -> TaxForm:
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
        is_corrected=corrected,
        fingerprint=f"fp{n}",
    )
    session.add(form)
    session.flush()
    src = form_type.upper()
    for cat, amount in (income or {}).items():
        session.add(
            IncomeItem(
                form_id=form.id,
                user_id=user_id,
                tax_year=year,
                category=cat,
                amount_minor=amount,
                source_form=src,
            )
        )
    for cat, amount in (memo or {}).items():
        session.add(
            IncomeItem(
                form_id=form.id,
                user_id=user_id,
                tax_year=year,
                category=cat,
                amount_minor=amount,
                source_form=src,
                is_memo=True,
            )
        )
    for cat, amount in (taxes or {}).items():
        session.add(
            TaxItem(
                form_id=form.id,
                user_id=user_id,
                tax_year=year,
                category=cat,
                amount_minor=amount,
                source_form=src,
            )
        )
    session.flush()
    return form


def _one(session: Session, **kw: object):  # type: ignore[no-untyped-def]
    (summary,) = run_tax_summary(session, TaxQuery(**kw))  # type: ignore[arg-type]
    return summary


def test_no_data_returns_empty(session: Session) -> None:
    assert run_tax_summary(session, TaxQuery()) == []
    assert list_tax_years(session, None) == []


def test_source_documents_only(session: Session) -> None:
    u = _user(session)
    _form(
        session,
        u,
        2024,
        "w2",
        income={"salary_wages": 10_000_000},
        taxes={
            "federal_income_tax_withheld": 1_500_000,
            "social_security_tax": 620_000,
            "medicare_tax": 145_000,
            "state_income_tax_withheld": 400_000,
        },
    )
    _form(session, u, 2024, "1099-int", income={"interest": 50_000})
    _form(
        session,
        u,
        2024,
        "1099-div",
        income={"ordinary_dividends": 200_000, "qualified_dividends": 150_000},
    )
    _form(session, u, 2024, "1099-b", income={"long_term_capital_gain": 300_000})
    s = _one(session)
    assert s.income_basis == "source_documents"
    assert s.income_by_category["salary_wages"] == 10_000_000
    # qualified dividends are a memo: listed, not added to the total
    assert s.income_by_category["qualified_dividends"] == 150_000
    assert s.memo_by_category == {"qualified_dividends": 150_000}
    assert s.total_income_minor == 10_000_000 + 50_000 + 200_000 + 300_000
    assert s.withheld["federal_income_tax_withheld"] == 1_500_000
    assert s.withheld["local_income_tax_withheld"] == 0
    assert s.fica == {"social_security_tax": 620_000, "medicare_tax": 145_000}
    assert s.liability == {}
    assert s.federal_effective_rate is None  # no return
    assert s.withholding_rate == round(1_500_000 / 10_550_000, 4)
    # Same basis as the headline "Total tax": estimated federal (no return) + FICA + state.
    assert s.headline is not None and s.headline.federal_basis == "estimate"
    assert s.headline.total_minor == s.headline.federal_minor + 620_000 + 145_000 + 400_000
    assert s.all_in_rate == round(s.headline.total_minor / 10_550_000, 4)
    assert (s.form_count, s.document_count) == (4, 4)
    assert s.warnings == []


def test_return_only_falls_back_to_return_income(session: Session) -> None:
    u = _user(session)
    _form(
        session,
        u,
        2024,
        "1040",
        income={"salary_wages": 9_000_000, "interest": 10_000},
        taxes={"total_federal_tax": 1_000_000, "total_payments": 1_200_000, "refund": 200_000},
    )
    s = _one(session)
    assert s.income_basis == "return"
    assert s.total_income_minor == 9_010_000
    assert any("No W-2/1099" in w for w in s.warnings)
    assert s.federal_effective_rate == round(1_000_000 / 9_010_000, 4)
    assert (s.payments_minor, s.refund_minor, s.amount_owed_minor) == (1_200_000, 200_000, None)
    assert s.refund_or_owed_derived is False


def test_return_is_authoritative_for_income_and_categories(session: Session) -> None:
    u = _user(session)
    _form(
        session,
        u,
        2024,
        "w2",
        income={"salary_wages": 8_000_000},
        taxes={"federal_income_tax_withheld": 900_000, "medicare_tax": 116_000},
    )
    _form(
        session,
        u,
        2024,
        "1040",
        income={"salary_wages": 8_000_000, "interest": 500_000, "ordinary_dividends": 2_000_000},
        memo={"qualified_dividends": 1_500_000},
        taxes={"total_federal_tax": 1_000_000},
    )
    s = _one(session)
    assert s.income_basis == "return"
    assert s.total_income_minor == 10_500_000  # the return, not the W-2 alone, not both
    assert s.income_by_category["interest"] == 500_000
    assert s.income_by_category["ordinary_dividends"] == 2_000_000
    assert s.withheld["federal_income_tax_withheld"] == 900_000
    assert s.fica["medicare_tax"] == 116_000
    assert s.liability == {"total_federal_tax": 1_000_000}
    assert s.federal_effective_rate == round(1_000_000 / 10_500_000, 4)
    assert any("1099 interest is $5,000.00 less than the return reports" in w for w in s.warnings)
    assert any("1099 dividends is $20,000.00 less" in w for w in s.warnings)
    assert not any("W-2 wages" in w for w in s.warnings)
    for w in s.warnings:  # privacy rule: no digit run of six or more
        assert not has_long_digit_run(w)


def test_source_more_than_return_warns(session: Session) -> None:
    u = _user(session)
    _form(session, u, 2024, "w2", income={"salary_wages": 8_500_000})
    _form(session, u, 2024, "1040", income={"salary_wages": 8_000_000})
    s = _one(session)
    assert s.total_income_minor == 8_000_000
    assert any("W-2 wages is $5,000.00 more than the return reports" in w for w in s.warnings)


def test_federal_withholding_falls_back_to_return_line_25d(session: Session) -> None:
    u = _user(session)
    _form(session, u, 2024, "w2", income={"salary_wages": 8_000_000})  # no box 2 imported
    _form(
        session,
        u,
        2024,
        "1040",
        income={"salary_wages": 8_000_000},
        taxes={"federal_income_tax_withheld": 1_100_000, "total_federal_tax": 1_000_000},
    )
    s = _one(session)
    assert s.withheld["federal_income_tax_withheld"] == 1_100_000


def test_source_withholding_preferred_and_mismatch_warns(session: Session) -> None:
    u = _user(session)
    _form(
        session,
        u,
        2024,
        "w2",
        income={"salary_wages": 8_000_000},
        taxes={"federal_income_tax_withheld": 900_000},
    )
    _form(
        session,
        u,
        2024,
        "1040",
        income={"salary_wages": 8_000_000},
        taxes={"federal_income_tax_withheld": 1_100_000},
    )
    s = _one(session)
    assert s.withheld["federal_income_tax_withheld"] == 900_000
    assert any("line 25d" in w for w in s.warnings)


def test_matching_wages_within_a_dollar_do_not_warn(session: Session) -> None:
    u = _user(session)
    _form(session, u, 2024, "w2", income={"salary_wages": 8_000_000})
    _form(session, u, 2024, "1040", income={"salary_wages": 8_000_050})
    assert _one(session).warnings == []


def test_net_capital_loss_without_return_is_floored(session: Session) -> None:
    u = _user(session)
    _form(session, u, 2024, "w2", income={"salary_wages": 5_000_000})
    _form(session, u, 2024, "1099-b", income={"short_term_capital_gain": -200_000})
    s = _one(session)
    assert s.income_by_category["short_term_capital_gain"] == -200_000  # within the limit
    assert s.total_income_minor == 4_800_000
    assert s.warnings == []


def test_large_capital_loss_without_return_is_limited_to_3000(session: Session) -> None:
    u = _user(session)
    _form(session, u, 2024, "w2", income={"salary_wages": 10_000_000})
    _form(
        session,
        u,
        2024,
        "1099-b",
        income={"short_term_capital_gain": -5_000_000, "long_term_capital_gain": 100_000},
    )
    s = _one(session)
    assert s.income_basis == "source_documents"
    assert s.total_income_minor == 10_000_000 - 300_000
    cats = s.income_by_category
    assert cats["short_term_capital_gain"] + cats["long_term_capital_gain"] == -300_000
    assert any("$3,000 annual limit" in w and "$46,000.00 is a carryover" in w for w in s.warnings)
    assert s.withholding_rate is None
    # No withholding at all, but the estimate is the headline basis, so the rate follows it.
    assert s.headline is not None and s.headline.federal_basis == "estimate"
    assert s.all_in_rate == round(s.headline.total_minor / s.total_income_minor, 4)


def test_capital_loss_limit_spreads_over_both_categories(session: Session) -> None:
    u = _user(session)
    _form(
        session,
        u,
        2024,
        "1099-b",
        income={"short_term_capital_gain": -500_000, "long_term_capital_gain": -500_000},
    )
    s = _one(session)
    cats = s.income_by_category
    assert cats["short_term_capital_gain"] + cats["long_term_capital_gain"] == -300_000
    assert s.total_income_minor == -300_000


def test_return_applies_loss_limit_and_source_is_compared_after_flooring(session: Session) -> None:
    u = _user(session)
    _form(session, u, 2024, "1099-b", income={"short_term_capital_gain": -5_000_000})
    _form(session, u, 2024, "1040", income={"long_term_capital_gain": -300_000})
    s = _one(session)
    assert s.total_income_minor == -300_000
    assert s.warnings == []  # -$50,000 on the 1099-B equals the -$3,000 on the return once limited


def test_multiple_returns_use_only_the_latest(session: Session) -> None:
    u = _user(session)
    _form(
        session,
        u,
        2024,
        "1040",
        income={"salary_wages": 10_000_000},
        taxes={"total_federal_tax": 1_400_000, "total_payments": 1_500_000, "refund": 100_000},
    )
    _form(
        session,
        u,
        2024,
        "1040",
        income={"salary_wages": 10_100_000},
        taxes={"total_federal_tax": 1_450_000, "total_payments": 1_500_000, "refund": 50_000},
    )
    s = _one(session)
    assert s.total_income_minor == 10_100_000  # not summed
    assert s.liability == {"total_federal_tax": 1_450_000}
    assert (s.payments_minor, s.refund_minor) == (1_500_000, 50_000)
    assert any("2 Form 1040 returns" in w for w in s.warnings)
    assert s.form_count == 2


def test_all_in_rate_does_not_double_count_additional_medicare(session: Session) -> None:
    u = _user(session)
    _form(
        session,
        u,
        2024,
        "w2",
        income={"salary_wages": 30_000_000},
        taxes={
            "federal_income_tax_withheld": 6_000_000,
            "social_security_tax": 1_044_000,
            "medicare_tax": 435_000 + 90_000,  # 1.45% plus the 0.9% withheld over $200k
        },
    )
    _form(
        session,
        u,
        2024,
        "1040",
        income={"salary_wages": 30_000_000},
        taxes={"total_federal_tax": 7_000_000, "additional_medicare_tax": 90_000},
    )
    s = _one(session)
    expected = (7_000_000 - 90_000) + 1_044_000 + 525_000
    assert s.all_in_rate == round(expected / 30_000_000, 4)


def test_all_in_rate_keeps_additional_medicare_without_w2_medicare(session: Session) -> None:
    u = _user(session)
    _form(
        session,
        u,
        2024,
        "1040",
        income={"salary_wages": 30_000_000},
        taxes={"total_federal_tax": 7_000_000, "additional_medicare_tax": 90_000},
    )
    assert _one(session).all_in_rate == round(7_000_000 / 30_000_000, 4)


def test_tax_exempt_memo_excluded_from_total(session: Session) -> None:
    u = _user(session)
    _form(session, u, 2024, "1099-int", income={"interest": 10_000}, memo={"other": 7_000})
    s = _one(session)
    assert s.total_income_minor == 10_000
    assert s.memo_by_category == {"tax_exempt": 7_000}
    assert s.income_by_category["other"] == 0


def test_derived_refund_and_owed(session: Session) -> None:
    u = _user(session)
    _form(
        session,
        u,
        2024,
        "1040",
        income={"salary_wages": 5_000_000},
        taxes={"total_federal_tax": 700_000, "total_payments": 900_000},
    )
    _form(
        session,
        u,
        2023,
        "1040",
        income={"salary_wages": 5_000_000},
        taxes={"total_federal_tax": 700_000, "total_payments": 650_000},
    )
    s24, s23 = run_tax_summary(session, TaxQuery())
    assert (s24.tax_year, s24.refund_minor, s24.amount_owed_minor) == (2024, 200_000, 0)
    assert s24.refund_or_owed_derived is True
    assert (s23.tax_year, s23.refund_minor, s23.amount_owed_minor) == (2023, 0, 50_000)
    assert any("derived" in w for w in s24.warnings)


def test_estimate_split_warns(session: Session) -> None:
    u = _user(session)
    _form(
        session,
        u,
        2024,
        "1040",
        income={"salary_wages": 5_000_000},
        taxes={"federal_tax_ordinary_est": 500_000, "federal_tax_preferential_est": 50_000},
    )
    assert any("estimate" in w for w in _one(session).warnings)


def test_rates_none_on_zero_income(session: Session) -> None:
    u = _user(session)
    _form(session, u, 2024, "w2", income={}, taxes={"federal_income_tax_withheld": 10_000})
    s = _one(session)
    assert s.total_income_minor == 0
    assert (s.federal_effective_rate, s.all_in_rate, s.withholding_rate) == (None, None, None)


def test_corrected_form_warns(session: Session) -> None:
    u = _user(session)
    _form(session, u, 2024, "w2", income={"salary_wages": 100}, corrected=True)
    assert any("corrected" in w for w in _one(session).warnings)


def test_users_are_isolated_and_aggregate_independently(session: Session) -> None:
    a, b = _user(session, "alice"), _user(session, "bob")
    _form(session, a, 2024, "w2", income={"salary_wages": 6_000_000})
    _form(session, b, 2024, "w2", income={"salary_wages": 4_000_000})
    only_a = _one(session, user_ids=[a])
    assert (only_a.user_id, only_a.total_income_minor) == (a, 6_000_000)
    both = _one(session)
    assert both.user_id is None and both.total_income_minor == 10_000_000
    assert both.form_count == 2
    per_user = run_tax_summary(session, TaxQuery(per_user=True))
    assert sorted((s.user_id, s.total_income_minor) for s in per_user) == [
        (a, 6_000_000),
        (b, 4_000_000),
    ]
    assert list_tax_years(session, [a]) == [2024]


def test_mixed_basis_across_users_is_resolved_per_user(session: Session) -> None:
    a, b = _user(session, "alice"), _user(session, "bob")
    _form(session, a, 2024, "w2", income={"salary_wages": 6_000_000})
    _form(session, b, 2024, "1040", income={"salary_wages": 4_000_000})
    _form(session, b, 2024, "w2", income={"salary_wages": 1_000_000})
    s = _one(session)
    assert s.total_income_minor == 10_000_000  # a: source only; b: the return (not the W-2)
    assert s.income_basis == "mixed"


def test_multi_year_sorted_desc_and_filter(session: Session) -> None:
    u = _user(session)
    for year in (2022, 2024, 2023):
        _form(session, u, year, "w2", income={"salary_wages": 1_000_000})
    assert [s.tax_year for s in run_tax_summary(session, TaxQuery())] == [2024, 2023, 2022]
    assert [s.tax_year for s in run_tax_summary(session, TaxQuery(tax_years=[2023]))] == [2023]
    assert list_tax_years(session, None) == [2024, 2023, 2022]


@pytest.mark.parametrize(
    "query",
    [
        TaxQuery(currency="EUR"),
        TaxQuery(tax_years=[1989]),
        TaxQuery(tax_years=[2101]),
    ],
)
def test_invalid_query_raises(session: Session, query: TaxQuery) -> None:
    with pytest.raises(ValueError):
        run_tax_summary(session, query)


def test_income_by_payer_excludes_returns_and_memo(session: Session) -> None:
    u = _user(session)
    _form(session, u, 2024, "w2", income={"salary_wages": 900}, payer="Acme")
    _form(session, u, 2024, "w2", income={"salary_wages": 100}, payer="Beta")
    _form(
        session,
        u,
        2024,
        "1099-div",
        income={"ordinary_dividends": 50, "qualified_dividends": 40},
        payer="Broker",
    )
    _form(session, u, 2024, "1040", income={"salary_wages": 1_000})
    rows = run_income_by_payer(session, TaxQuery())
    assert [(r.payer_name, r.category, r.amount_minor) for r in rows] == [
        ("Acme", "salary_wages", 900),
        ("Beta", "salary_wages", 100),
        ("Broker", "ordinary_dividends", 50),
    ]


def test_documents_listing_and_detail(session: Session) -> None:
    a, b = _user(session, "alice"), _user(session, "bob")
    f1 = _form(session, a, 2023, "w2", income={"salary_wages": 1})
    _form(session, a, 2024, "w2", income={"salary_wages": 1})
    _form(session, b, 2024, "w2", income={"salary_wages": 1})
    docs = list_tax_documents(session, user_ids=[a], tax_year=None)
    assert [d.tax_year for d in docs] == [2024, 2023]
    assert len(list_tax_documents(session, user_ids=None, tax_year=2024)) == 2
    detail = get_tax_document(session, f1.document_id)
    assert detail is not None
    assert detail.forms[0].income_items[0].amount_minor == 1
    assert get_tax_document(session, 99_999) is None
