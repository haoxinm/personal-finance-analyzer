"""Per-user federal income tax estimate from source forms and manual inputs (local only).

**What it is.** A simple, reproducible estimate of one user's federal tax for one year, computed
from the W-2 / 1099 source forms (`tax_query.source_figures`) plus the user's `tax_profiles` row
(filing status, deductions, credits, other taxes, payments that no source form carries). It is
never copied from a Form 1040: when a return exists it is shown next to the estimate as a
cross-check (`CrossCheck`) and used for nothing else, except as the default filing status when the
user has no profile.

**Method** (`compute`, pure; `run_tax_estimate` gathers the inputs):

1. net capital result = short term + long term - carryover; a net loss is limited to the year's
   capital-loss limit ($3,000, married filing separately $1,500) and the rest is reported as the
   carryover to next year;
2. gross income = wages + interest + ordinary dividends (qualified dividends are inside them) +
   self-employment + other income + the limited capital result + the profile's other income;
3. self-employment tax per person: 92.35% of self-employment income; 12.4% on the part that fits
   under the Social Security wage base after that person's W-2 Social Security wages, plus 2.9%;
   none below $400 of net earnings. Half of it is an adjustment;
4. AGI = gross - adjustments - half the self-employment tax; deduction = the standard deduction
   (plus the aged / blind amounts) or the itemized total the user entered; for 2025-2028 the
   senior deduction ($6,000 per qualifying person aged 65+, reduced by 6% of AGI over $75,000,
   $150,000 joint; IRC 151(d)(5)(C)) comes on top for both modes; taxable income =
   max(AGI - deduction - senior deduction, 0);
5. preferential income = qualified dividends + the long-term gain that survives the net capital
   result; income tax = the IRS Qualified Dividends and Capital Gain Tax Worksheet
   (`core/tax_calc.py`, Tax Table below $100,000, whole dollars);
6. other taxes on the return: net investment income tax 3.8% x min(net investment income, AGI -
   threshold) (Form 8960; net investment income = interest + ordinary dividends + the counted
   capital result, not below 0); Additional Medicare Tax 0.9% on Medicare wages plus
   self-employment earnings above the filing-status threshold (Form 8959);
7. total tax (never negative) = max(income tax - nonrefundable credits, 0) + self-employment tax +
   net investment income tax + Additional Medicare Tax + other taxes. Refundable credits are
   *payments*, like lines 27-31 of the 1040, and so are the Additional Medicare Tax already withheld
   (W-2 box 6 above 1.45% of Medicare wages; Form 8959 line 24) and the excess Social Security tax
   withheld by two or more employers (Schedule 3 line 11). payments = federal tax withheld +
   estimated payments + other payments + refundable credits + those two; result = payments - total
   tax (positive: refund, negative: owed).

**Medicare wages.** W-2 box 5 is not stored. Medicare wages are approximated as box 1 wages plus the
box 12 elective deferrals (which box 1 excludes and box 5 includes); the Additional Medicare Tax
withheld is box 6 less 1.45% of that approximation, so an unusual box 5 (a taxable fringe benefit,
dependent-care benefits above $5,000) skews it slightly.

**W-2 box 12.** Deferrals (codes D, E, F, G, S) and employer HSA contributions (W) are already
excluded from box 1, so they are memo items here: shown, checked against the year's elective
deferral limit, never subtracted from wages again.

**Married filing jointly.** When the profile is ``mfj`` with a spouse (``joint_with_user_id``;
either spouse's profile may carry the link) the household's items and both profiles' manual inputs
are summed and the tax computed once. The per-user view is an *allocation* of the household tax by
share of gross income (the user's own payments and result follow from it). Profiles that disagree
raise `EstimateConflict`.

**Not modeled** (`NOT_MODELED`): the estimate says so rather than guess. Units: integer cents;
rates are fractions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from spend_analyzer.analytics import tax_query
from spend_analyzer.analytics.tax_query import ReturnFigures, SourceFigures
from spend_analyzer.core import tax_calc, tax_params
from spend_analyzer.core.tax_params import FILING_STATUSES, FederalParams
from spend_analyzer.db.models import TaxProfile

DeductionMode = Literal["standard", "itemized"]
FilingStatusSource = Literal["profile", "return", "default"]
Scope = Literal["individual", "household_allocation"]

#: What the estimate leaves out; shown with every result.
NOT_MODELED: tuple[str, ...] = (
    "Alternative minimum tax.",
    "Net investment income is only interest, ordinary dividends and capital gains: rental, "
    "royalty, passive business income and annuities are not included, and deductions allocable to "
    "investment income (investment interest, state tax) are not subtracted.",
    "Credits that depend on your situation (child tax credit, earned income credit, education "
    "credits, saver's credit): enter their total in the profile (refundable ones, such as the earned "
    "income credit, as refundable credits: they count as payments, not as negative tax).",
    "Qualified business income deduction.",
    "28% rate gains and unrecaptured section 1250 gains.",
    "Phase-outs of deductions and credits, and the SALT cap: enter your itemized total after them "
    "(only the senior deduction's phase-out is modeled).",
    "2025-2028 deductions for qualified tips (up to $25,000), qualified overtime (up to $12,500, "
    "$25,000 joint) and car loan interest (up to $10,000), all phased out above $150,000 "
    "($300,000 joint) of income, $100,000 ($200,000 joint) for the loan interest: take the amount "
    "from Schedule 1-A and enter it as an adjustment to income. The estimate then lowers AGI by "
    "it, which the real deduction does not, so the NIIT and the senior deduction's phase-out can be "
    "slightly understated.",
    "Taxable Social Security benefits, IRA and pension income, rental and partnership income: "
    "enter the taxable amount as other income.",
    "State and local income tax.",
)

_BP = 10_000
_SE_NET_FACTOR_BP = 9_235
_SE_MIN_NET_MINOR = 40_000  # no self-employment tax below $400 of net earnings
_SS_RATE_BP = 1_240
_MEDICARE_RATE_BP = 290
_EMPLOYEE_SS_RATE_BP = 620
_ADDITIONAL_MEDICARE_RATE_BP = 90
_NIIT_RATE_BP = 380
#: Boxes that can be checked for the aged / blind additional standard deduction (two per person,
#: four on a joint return) and the people aged 65+ who can claim the senior deduction.
MAX_AGED_BLIND_JOINT = 4
MAX_AGED_BLIND_SINGLE = 2
MAX_SENIORS_JOINT = 2
MAX_SENIORS_SINGLE = 1


def profile_count_limits(filing_status: str, *, household_total: bool) -> tuple[int, int]:
    """(aged / blind boxes, people aged 65+) one profile may count. A profile describes its own
    user (2, 1), except a married-filing-jointly profile that stands for the whole household
    because the spouse has no profile of their own: it carries both spouses' boxes (4, 2)."""
    if filing_status == "mfj" and household_total:
        return MAX_AGED_BLIND_JOINT, MAX_SENIORS_JOINT
    return MAX_AGED_BLIND_SINGLE, MAX_SENIORS_SINGLE


def _person_boxes(profile: TaxProfile | None, limits: tuple[int, int]) -> tuple[int, int]:
    """(aged / blind boxes, people aged 65+) one profile contributes, clamped to `limits`."""
    if profile is None:
        return 0, 0
    boxes = min(max(profile.age65_or_blind_count, 0), limits[0])
    return boxes, min(max(profile.age65_count, 0), limits[1], boxes)


class EstimateError(ValueError):
    """Base class of the errors `run_tax_estimate` raises."""


class EstimateUnavailable(EstimateError):
    """There are no tax parameters for the requested year (the API answers 422)."""


class EstimateConflict(EstimateError):
    """The spouses' profiles disagree on status or spouse (the API answers 409)."""


def _bp(amount: int, rate_bp: int) -> int:
    """`amount` x `rate_bp` / 10,000 rounded half up (amounts here are never negative)."""
    return (amount * rate_bp + _BP // 2) // _BP


@dataclass(frozen=True, slots=True)
class PersonInputs:
    """Figures that are computed per person: self-employment tax has its own wage-base cap and
    the deferral limit applies to each person."""

    user_id: int | None
    wages_minor: int
    self_employment_minor: int
    #: Social Security wages (W-2 box 3) used for the wage-base cap: implied by the box 4 tax
    #: withheld when present, else wages plus deferrals.
    social_security_wages_minor: int
    retirement_deferral_minor: int = 0
    #: Of `retirement_deferral_minor`, the 457(b) deferrals (box 12 code G), which have their own
    #: annual limit.
    deferral_457b_minor: int = 0
    #: W-2 box 6 and box 4 totals, and how many employers withheld Social Security tax.
    medicare_tax_withheld_minor: int = 0
    social_security_tax_withheld_minor: int = 0
    social_security_employers: int = 0
    #: Medicare wages (W-2 box 5, not stored): ``None`` = approximated, see the module docstring.
    medicare_wages_minor: int | None = None

    @property
    def medicare_wages(self) -> int:
        if self.medicare_wages_minor is not None:
            return self.medicare_wages_minor
        return self.wages_minor + self.retirement_deferral_minor


@dataclass(frozen=True, slots=True)
class EstimateInputs:
    """Everything `compute` needs (cents). Household totals for a joint return."""

    tax_year: int
    filing_status: str
    persons: tuple[PersonInputs, ...]
    interest_minor: int = 0
    ordinary_dividends_minor: int = 0
    qualified_dividends_minor: int = 0
    short_term_gain_minor: int = 0
    long_term_gain_minor: int = 0
    other_income_minor: int = 0  # non-memo "other" income from the source forms
    profile_other_income_minor: int = 0
    capital_loss_carryover_minor: int = 0
    adjustments_minor: int = 0
    deduction_mode: DeductionMode = "standard"
    itemized_deductions_minor: int = 0
    aged_or_blind_count: int = 0
    #: People (taxpayer, and spouse on a joint return) aged 65 or over: the senior deduction.
    senior_count: int = 0
    nonrefundable_credits_minor: int = 0
    refundable_credits_minor: int = 0
    other_taxes_minor: int = 0
    federal_withheld_minor: int = 0
    estimated_payments_minor: int = 0
    other_payments_minor: int = 0
    hsa_employer_minor: int = 0

    @property
    def wages_minor(self) -> int:
        return sum(p.wages_minor for p in self.persons)

    @property
    def self_employment_minor(self) -> int:
        return sum(p.self_employment_minor for p in self.persons)

    @property
    def retirement_deferral_minor(self) -> int:
        return sum(p.retirement_deferral_minor for p in self.persons)


@dataclass(frozen=True, slots=True)
class EstimateResult:
    """The computed return lines (cents). See the module docstring for each step."""

    tax_year: int
    filing_status: str
    # income
    wages_minor: int
    interest_minor: int
    ordinary_dividends_minor: int
    qualified_dividends_minor: int
    short_term_gain_minor: int
    long_term_gain_minor: int
    capital_loss_carryover_in_minor: int
    capital_result_counted_minor: int  # net capital result after the loss limit
    capital_loss_carryover_out_minor: int
    self_employment_minor: int
    other_income_minor: int
    profile_other_income_minor: int
    gross_income_minor: int
    # AGI and deduction
    adjustments_minor: int
    se_tax_deduction_minor: int
    agi_minor: int
    deduction_mode: DeductionMode
    deduction_minor: int  # standard or itemized (before the senior deduction)
    senior_deduction_minor: int
    taxable_income_minor: int  # max(AGI - deduction - senior deduction, 0)
    preferential_income_minor: int
    # tax: income tax - nonrefundable credits applied + SE + NIIT + Additional Medicare + other
    income_tax_minor: int
    ordinary_tax_minor: int
    preferential_tax_minor: int
    #: The nonrefundable credits actually used (never more than the income tax); the amount the
    #: user entered is `nonrefundable_credits_entered_minor`.
    nonrefundable_credits_minor: int
    nonrefundable_credits_entered_minor: int
    se_tax_minor: int
    net_investment_income_tax_minor: int
    additional_medicare_tax_minor: int
    other_taxes_minor: int
    total_tax_minor: int  # never negative
    # payments (refundable credits, Additional Medicare Tax withheld and the excess Social
    # Security credit are payments, like 1040 lines 25-31) and result
    federal_withheld_minor: int
    estimated_payments_minor: int
    other_payments_minor: int
    refundable_credits_minor: int
    additional_medicare_withheld_minor: int
    excess_social_security_credit_minor: int
    total_payments_minor: int
    result_minor: int  # payments - total tax: positive refund, negative owed
    # rates (fractions; None when undefined)
    effective_rate: float | None
    marginal_rate: float | None
    preferential_rate: float | None
    # memo (W-2 box 12): not part of the computation
    retirement_deferral_minor: int
    hsa_employer_minor: int
    warnings: tuple[str, ...] = ()
    not_modeled: tuple[str, ...] = NOT_MODELED

    @property
    def refund_minor(self) -> int:
        return max(self.result_minor, 0)

    @property
    def owed_minor(self) -> int:
        return max(-self.result_minor, 0)


def _fmt(minor: int) -> str:
    return f"${minor // 100:,}"


def _se_net(person: PersonInputs) -> int:
    """Net earnings from self-employment (Schedule SE line 6), 0 below the $400 floor."""
    net = _bp(person.self_employment_minor, _SE_NET_FACTOR_BP)
    return net if net >= _SE_MIN_NET_MINOR else 0


def _se_tax(person: PersonInputs, params: FederalParams) -> int:
    net = _se_net(person)
    if net == 0:
        return 0
    ss_room = max(params.ss_wage_base_minor - person.social_security_wages_minor, 0)
    return _bp(min(net, ss_room), _SS_RATE_BP) + _bp(net, _MEDICARE_RATE_BP)


def additional_medicare(persons: Sequence[PersonInputs], threshold_minor: int) -> tuple[int, int]:
    """(Additional Medicare Tax owed, part of it already withheld) as on Form 8959.

    Owed: 0.9% of the Medicare wages above the filing-status threshold, plus 0.9% of the net
    self-employment earnings above whatever part of the threshold the wages left unused.
    Withheld: per person, W-2 box 6 beyond 1.45% of that person's Medicare wages (employers
    withhold the extra 0.9% on wages over $200,000 whatever the filing status, so it can be
    more or less than the tax owed); rounding noise below $1 is ignored.
    """
    wages = sum(p.medicare_wages for p in persons)
    se_net = sum(_se_net(p) for p in persons)
    wage_excess = max(wages - threshold_minor, 0)
    se_excess = max(se_net - max(threshold_minor - wages, 0), 0)
    owed = _bp(wage_excess + se_excess, _ADDITIONAL_MEDICARE_RATE_BP)
    return owed, sum(additional_medicare_withheld(p) for p in persons)


def additional_medicare_withheld(person: PersonInputs) -> int:
    """The Additional Medicare Tax inside this person's W-2 box 6 totals (see `additional_medicare`)."""
    return tax_calc.additional_medicare_withheld(
        person.medicare_tax_withheld_minor, person.medicare_wages
    )


def excess_social_security(persons: Sequence[PersonInputs], params: FederalParams) -> int:
    """The refundable credit for Social Security tax withheld beyond the annual maximum.

    Per person, and only when two or more employers withheld it (box 4 totals above 6.2% of the
    wage base): one employer's over-withholding is the employer's to refund, not a credit.
    """
    maximum = _bp(params.ss_wage_base_minor, _EMPLOYEE_SS_RATE_BP)
    return sum(
        p.social_security_tax_withheld_minor - maximum
        for p in persons
        if p.social_security_employers >= 2 and p.social_security_tax_withheld_minor > maximum
    )


def compute(inputs: EstimateInputs, params: FederalParams) -> EstimateResult:
    """The estimate for `inputs` under `params` (pure; no I/O). Amounts are cents.

    Raises:
        ValueError: an unknown filing status, or `params` for a different tax year.
    """
    status = inputs.filing_status
    if status not in params.tables or params.tax_year != inputs.tax_year:
        raise ValueError(f"no parameters for {status!r} in {inputs.tax_year}")
    warnings: list[str] = []

    # 1. capital gains and losses
    net_capital = (
        inputs.short_term_gain_minor
        + inputs.long_term_gain_minor
        - inputs.capital_loss_carryover_minor
    )
    limit = params.capital_loss_limit(status)
    counted = max(net_capital, -limit)
    carryover_out = max(-net_capital - limit, 0)
    if carryover_out:
        warnings.append(
            f"Net capital loss is over the {_fmt(limit)} annual limit: only {_fmt(limit)} is "
            f"deducted and {_fmt(carryover_out)} carries over to {inputs.tax_year + 1}."
        )

    # 2-3. income, self-employment tax, AGI
    wages = inputs.wages_minor
    se_income = inputs.self_employment_minor
    gross = (
        wages
        + inputs.interest_minor
        + inputs.ordinary_dividends_minor
        + se_income
        + inputs.other_income_minor
        + counted
        + inputs.profile_other_income_minor
    )
    se_tax = sum(_se_tax(p, params) for p in inputs.persons)
    se_deduction = _bp(se_tax, _BP // 2)
    agi = gross - inputs.adjustments_minor - se_deduction

    # 4. deduction
    box_limit = MAX_AGED_BLIND_JOINT if status == "mfj" else MAX_AGED_BLIND_SINGLE
    boxes = min(max(inputs.aged_or_blind_count, 0), box_limit)
    if boxes < inputs.aged_or_blind_count:
        warnings.append(
            f"Age 65 / blind boxes: at most {box_limit} apply to this filing status; "
            f"{boxes} are used."
        )
    if inputs.deduction_mode == "itemized":
        deduction = inputs.itemized_deductions_minor
        standard = params.standard_deduction(status, boxes)
        if deduction < standard:
            warnings.append(
                f"Itemized deductions ({_fmt(deduction)}) are below the standard deduction "
                f"({_fmt(standard)}); the standard deduction would give a lower tax."
            )
    else:
        deduction = params.standard_deduction(status, boxes)
    senior = params.senior_deduction(status, inputs.senior_count, agi)
    if inputs.senior_count > 0 and status == "mfs" and params.senior_deduction_minor > 0:
        warnings.append(
            "The senior deduction is not available to married persons filing separately."
        )
    taxable = max(agi - deduction - senior, 0)

    # 5. income tax: Qualified Dividends and Capital Gain Tax Worksheet
    qualified = min(inputs.qualified_dividends_minor, inputs.ordinary_dividends_minor)
    long_term_pref = max(0, min(inputs.long_term_gain_minor, net_capital))
    preferential = min(qualified + long_term_pref, taxable)
    income_tax = tax_calc.worksheet_tax(
        tax_year=inputs.tax_year,
        filing_status=status,
        taxable_income_minor=taxable,
        qualified_dividends_minor=qualified,
        net_ltcg_minor=long_term_pref,
    )
    assert income_tax is not None  # the table exists: checked above
    split = tax_calc.estimate_tax_split(
        tax_year=inputs.tax_year,
        filing_status=status,
        taxable_income_minor=taxable,
        qualified_dividends_minor=qualified,
        net_ltcg_minor=long_term_pref,
        income_tax_minor=income_tax,
    )
    assert split is not None

    # 6. other taxes on the return, credits, payments, result
    nonrefundable_used = min(inputs.nonrefundable_credits_minor, income_tax)
    if nonrefundable_used < inputs.nonrefundable_credits_minor:
        warnings.append(
            f"Nonrefundable credits ({_fmt(inputs.nonrefundable_credits_minor)}) are more than the "
            f"income tax: only {_fmt(nonrefundable_used)} is used (the rest does not reduce other "
            "taxes and is not refunded unless the credit is refundable)."
        )
    investment = max(inputs.interest_minor + inputs.ordinary_dividends_minor + counted, 0)
    niit_threshold = params.niit_threshold_minor[status]
    niit = _bp(min(investment, max(agi - niit_threshold, 0)), _NIIT_RATE_BP)
    addl_medicare, addl_medicare_withheld = additional_medicare(
        inputs.persons, params.additional_medicare_threshold_minor[status]
    )
    excess_ss = excess_social_security(inputs.persons, params)
    total_tax = (
        income_tax - nonrefundable_used + se_tax + niit + addl_medicare + inputs.other_taxes_minor
    )
    payments = (
        inputs.federal_withheld_minor
        + inputs.estimated_payments_minor
        + inputs.other_payments_minor
        + inputs.refundable_credits_minor
        + addl_medicare_withheld
        + excess_ss
    )

    # rates
    ordinary_taxable = max(taxable - preferential, 0)
    marginal = tax_calc.marginal_rate_bp(inputs.tax_year, status, ordinary_taxable)
    pref_rate = (
        tax_calc.preferential_rate_bp(inputs.tax_year, status, taxable)
        if preferential > 0
        else None
    )

    # warnings that only inform (nothing here changes a number)
    multiple = len(inputs.persons) > 1
    for person in inputs.persons:
        who = "" if not multiple or person.user_id is None else f" (user {person.user_id})"
        limit_text = _fmt(params.elective_deferral_limit_minor)
        elective = person.retirement_deferral_minor - person.deferral_457b_minor
        if elective > params.elective_deferral_limit_minor:
            warnings.append(
                f"Pre-tax 401(k), 403(b), SEP and SIMPLE deferrals in W-2 box 12{who} "
                f"({_fmt(elective)}) are over the {inputs.tax_year} limit of {limit_text}. The "
                "limit is higher if you are 50 or older (catch-up contributions; check the IRS "
                "figure), and Roth deferrals (codes AA, BB) are not read from the W-2, so the "
                "total may be higher still; otherwise check for an excess deferral."
            )
        if person.deferral_457b_minor > params.elective_deferral_limit_minor:
            warnings.append(
                f"457(b) deferrals in W-2 box 12 code G{who} ({_fmt(person.deferral_457b_minor)}) "
                f"are over the {inputs.tax_year} limit of {limit_text}, which applies to 457(b) "
                "plans separately from the 401(k) limit (higher with catch-up contributions)."
            )
    if addl_medicare or addl_medicare_withheld:
        warnings.append(
            f"Additional Medicare Tax of {_fmt(addl_medicare)} is included; "
            f"{_fmt(addl_medicare_withheld)} was already withheld (W-2 box 6 beyond 1.45% of "
            "Medicare wages, which are approximated as box 1 wages plus deferrals because box 5 is "
            "not stored)."
        )
    if niit:
        warnings.append(
            f"Net investment income tax of {_fmt(niit)} is included "
            f"(3.8% of the lesser of {_fmt(investment)} of interest, dividends and gains and the "
            f"{_fmt(max(agi - niit_threshold, 0))} by which income is over {_fmt(niit_threshold)})."
        )
    if excess_ss:
        warnings.append(
            f"{_fmt(excess_ss)} of Social Security tax was withheld above the annual maximum by "
            "more than one employer; it is credited as a payment (Schedule 3 line 11)."
        )
    if qualified < inputs.qualified_dividends_minor:
        warnings.append("Qualified dividends exceed ordinary dividends; the excess is ignored.")

    return EstimateResult(
        tax_year=inputs.tax_year,
        filing_status=status,
        wages_minor=wages,
        interest_minor=inputs.interest_minor,
        ordinary_dividends_minor=inputs.ordinary_dividends_minor,
        qualified_dividends_minor=qualified,
        short_term_gain_minor=inputs.short_term_gain_minor,
        long_term_gain_minor=inputs.long_term_gain_minor,
        capital_loss_carryover_in_minor=inputs.capital_loss_carryover_minor,
        capital_result_counted_minor=counted,
        capital_loss_carryover_out_minor=carryover_out,
        self_employment_minor=se_income,
        other_income_minor=inputs.other_income_minor,
        profile_other_income_minor=inputs.profile_other_income_minor,
        gross_income_minor=gross,
        adjustments_minor=inputs.adjustments_minor,
        se_tax_deduction_minor=se_deduction,
        agi_minor=agi,
        deduction_mode=inputs.deduction_mode,
        deduction_minor=deduction,
        senior_deduction_minor=senior,
        taxable_income_minor=taxable,
        preferential_income_minor=preferential,
        income_tax_minor=income_tax,
        ordinary_tax_minor=split.ordinary_minor,
        preferential_tax_minor=split.preferential_minor,
        nonrefundable_credits_minor=nonrefundable_used,
        nonrefundable_credits_entered_minor=inputs.nonrefundable_credits_minor,
        se_tax_minor=se_tax,
        net_investment_income_tax_minor=niit,
        additional_medicare_tax_minor=addl_medicare,
        other_taxes_minor=inputs.other_taxes_minor,
        total_tax_minor=total_tax,
        federal_withheld_minor=inputs.federal_withheld_minor,
        estimated_payments_minor=inputs.estimated_payments_minor,
        other_payments_minor=inputs.other_payments_minor,
        refundable_credits_minor=inputs.refundable_credits_minor,
        additional_medicare_withheld_minor=addl_medicare_withheld,
        excess_social_security_credit_minor=excess_ss,
        total_payments_minor=payments,
        result_minor=payments - total_tax,
        effective_rate=round(total_tax / gross, 4) if gross > 0 else None,
        marginal_rate=None if marginal is None else marginal / _BP,
        preferential_rate=None if pref_rate is None else pref_rate / _BP,
        retirement_deferral_minor=inputs.retirement_deferral_minor,
        hsa_employer_minor=inputs.hsa_employer_minor,
        warnings=tuple(warnings),
    )


# --- gathering inputs ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrossCheck:
    """The latest Form 1040 for the year next to the estimate (never an input to it). Deltas are
    estimate minus return; a field is ``None`` when the return did not print it."""

    return_filing_status: str | None
    return_total_tax_minor: int | None
    return_withheld_minor: int | None
    return_refund_minor: int | None
    return_owed_minor: int | None
    return_result_minor: int | None  # refund (+) / owed (-)
    delta_total_tax_minor: int | None
    delta_withheld_minor: int | None
    delta_result_minor: int | None
    #: The estimate compared is the household's (a joint return covers both spouses).
    household: bool


@dataclass(frozen=True, slots=True)
class UserEstimate:
    """One user's view: the computation (the whole household's when joint) and this user's part.

    For ``scope == "individual"`` the user's figures are the computation's. For
    ``"household_allocation"`` ``user_total_tax_minor`` is an *allocation* of the household tax by
    share of gross income (the shares sum to the household tax); payments are the user's own.
    """

    user_id: int
    tax_year: int
    scope: Scope
    household_user_ids: tuple[int, ...]
    filing_status_source: FilingStatusSource
    calc: EstimateResult
    user_gross_income_minor: int
    share: float | None  # None for an individual estimate
    user_total_tax_minor: int
    user_payments_minor: int
    user_result_minor: int
    #: The part of this user's W-2 Medicare tax that is Additional Medicare Tax (already inside
    #: FICA, so a headline that adds FICA must not count it again).
    user_additional_medicare_withheld_minor: int
    cross_check: CrossCheck | None
    parameters_source: str
    parameters_version: int
    parameter_sources: tuple[str, ...]
    warnings: tuple[str, ...] = field(default=())


def get_profile(session: Session, user_id: int, tax_year: int) -> TaxProfile | None:
    """The saved profile of `user_id` for `tax_year`, if any."""
    return session.scalar(
        select(TaxProfile).where(TaxProfile.user_id == user_id, TaxProfile.tax_year == tax_year)
    )


def _spouse_of(session: Session, user_id: int, tax_year: int) -> int | None:
    """The spouse linked to `user_id` by an ``mfj`` profile on either side, or `None`.

    Raises:
        EstimateConflict: the two profiles disagree (the spouse has a profile that is not ``mfj``
            or names someone else; or two different users name this user).
    """
    own = get_profile(session, user_id, tax_year)
    pointing = list(
        session.scalars(
            select(TaxProfile).where(
                TaxProfile.tax_year == tax_year, TaxProfile.joint_with_user_id == user_id
            )
        )
    )
    spouse: int | None = None
    if own is not None and own.joint_with_user_id is not None:
        if own.filing_status != "mfj":
            raise EstimateConflict(
                "A spouse is linked but the filing status is not married filing jointly."
            )
        spouse = own.joint_with_user_id
        other = get_profile(session, spouse, tax_year)
        if other is not None and (
            other.filing_status != "mfj" or other.joint_with_user_id not in (None, user_id)
        ):
            raise EstimateConflict(
                f"The profile of user {spouse} does not match: both spouses must be married "
                "filing jointly and name each other."
            )
    elif pointing:
        users = {p.user_id for p in pointing}
        if len(users) > 1:
            raise EstimateConflict("More than one user names this user as their spouse.")
        (spouse,) = users
        if pointing[0].filing_status != "mfj":
            raise EstimateConflict(
                f"The profile of user {spouse} links this user without being married filing jointly."
            )
        if own is not None and own.filing_status != "mfj":
            raise EstimateConflict(
                f"User {spouse}'s profile is joint with this user, but this user's profile is "
                f"{own.filing_status}."
            )
    return spouse


def _person(figs: SourceFigures) -> PersonInputs:
    wages = figs.income.get("salary_wages", 0)
    deferral = figs.income.get("retirement_deferral", 0)
    ss_tax = figs.tax.get("social_security_tax", 0)
    ss_wages = (
        (ss_tax * _BP + _EMPLOYEE_SS_RATE_BP // 2) // _EMPLOYEE_SS_RATE_BP
        if ss_tax > 0
        else wages + deferral
    )
    return PersonInputs(
        user_id=figs.user_id,
        wages_minor=wages,
        self_employment_minor=figs.income.get("self_employment", 0),
        social_security_wages_minor=ss_wages,
        retirement_deferral_minor=deferral,
        deferral_457b_minor=figs.deferral_457b_minor,
        medicare_tax_withheld_minor=figs.tax.get("medicare_tax", 0),
        social_security_tax_withheld_minor=ss_tax,
        social_security_employers=figs.social_security_employers,
    )


def _cross_check(
    figs: SourceFigures, calc: EstimateResult, *, household: bool
) -> CrossCheck | None:
    ret: ReturnFigures | None = figs.return_figures
    if ret is None:
        return None
    result: int | None = None
    if ret.refund_minor is not None or ret.amount_owed_minor is not None:
        result = (ret.refund_minor or 0) - (ret.amount_owed_minor or 0)
    return CrossCheck(
        return_filing_status=ret.filing_status,
        return_total_tax_minor=ret.total_tax_minor,
        return_withheld_minor=ret.withheld_minor,
        return_refund_minor=ret.refund_minor,
        return_owed_minor=ret.amount_owed_minor,
        return_result_minor=result,
        delta_total_tax_minor=(
            None if ret.total_tax_minor is None else calc.total_tax_minor - ret.total_tax_minor
        ),
        delta_withheld_minor=(
            None if ret.withheld_minor is None else calc.federal_withheld_minor - ret.withheld_minor
        ),
        delta_result_minor=None if result is None else calc.result_minor - result,
        household=household,
    )


def _gross_share_base(inputs_income: dict[str, int], profile: TaxProfile | None) -> int:
    """A user's share of the household gross income: wages, interest, ordinary dividends,
    self-employment, other income and capital gains (a net loss counts as zero)."""
    capital = inputs_income.get("short_term_capital_gain", 0) + inputs_income.get(
        "long_term_capital_gain", 0
    )
    return (
        sum(
            inputs_income.get(c, 0)
            for c in ("salary_wages", "interest", "ordinary_dividends", "self_employment", "other")
        )
        + max(capital, 0)
        + (profile.other_income_minor if profile else 0)
    )


def _allocate(total: int, bases: list[int]) -> list[int]:
    """Split `total` by `bases` (equal shares when the bases sum to zero); the parts sum to
    `total` exactly (the last one takes the rounding remainder)."""
    weight = sum(bases)
    if weight <= 0:
        bases, weight = [1] * len(bases), len(bases)
    parts = [(total * b * 2 + weight) // (weight * 2) for b in bases[:-1]]
    return [*parts, total - sum(parts)]


def run_tax_estimate(session: Session, user_id: int, tax_year: int) -> UserEstimate:
    """Estimate `user_id`'s federal tax for `tax_year` (see the module docstring).

    Raises:
        EstimateUnavailable: no tax parameters for `tax_year`.
        EstimateConflict: the spouses' profiles disagree.
        ValueError: a tax year outside 1990..2100 or a non-int id (from `tax_query`).
    """
    params = tax_calc.params_for(tax_year)
    if params is None:
        raise EstimateUnavailable(
            f"No federal tax parameters for {tax_year} "
            f"(supported: {', '.join(map(str, tax_calc.supported_years()))})."
        )
    spouse = _spouse_of(session, user_id, tax_year)
    members = sorted({user_id, spouse} if spouse is not None else {user_id})
    figures = [tax_query.source_figures(session, uid, tax_year) for uid in members]
    profiles = [get_profile(session, uid, tax_year) for uid in members]
    notes: list[str] = []

    # filing status: profile, else the user's own return, else single
    own_idx = members.index(user_id)
    own_profile = profiles[own_idx]
    own_return = figures[own_idx].return_figures
    status: str
    status_source: FilingStatusSource
    if len(members) > 1:
        status, status_source = "mfj", "profile"
    elif own_profile is not None:
        status, status_source = own_profile.filing_status, "profile"
    elif own_return is not None and own_return.filing_status in FILING_STATUSES:
        status, status_source = own_return.filing_status, "return"
        notes.append("No tax profile: the filing status is taken from the Form 1040.")
    else:
        status, status_source = "single", "default"
        notes.append("No tax profile: filing status single and the standard deduction are assumed.")

    def total(attr: str) -> int:
        return sum(getattr(p, attr) for p in profiles if p is not None)

    income: dict[str, int] = {}
    tax: dict[str, int] = {}
    for fig in figures:
        for cat, amount in fig.income.items():
            income[cat] = income.get(cat, 0) + amount
        for cat, amount in fig.tax.items():
            tax[cat] = tax.get(cat, 0) + amount
    itemized_any = any(p is not None and p.deduction_mode == "itemized" for p in profiles)
    if len({p.deduction_mode for p in profiles if p is not None}) > 1:
        notes.append("The spouses chose different deduction modes; itemized is used.")
    if sum(1 for p in profiles if p is not None and p.itemized_deductions_minor > 0) > 1:
        notes.append(
            "Both spouses entered itemized deductions: the two amounts are added together on the "
            "joint return. Enter each spouse's own share, or the household total on one profile "
            "and nothing on the other."
        )
    # A profile describes its own user (at most 2 aged / blind boxes, 1 person aged 65+), so two
    # profiles of a household are added together. A single mfj profile (the spouse has none of
    # their own) is the household total: up to 4 boxes and 2 people aged 65+. Rows saved with more
    # than the limit allows are clamped, with a note.
    count_limits = profile_count_limits(
        status, household_total=sum(1 for p in profiles if p is not None) <= 1
    )
    person_boxes = [_person_boxes(p, count_limits) for p in profiles]
    if any(
        p is not None
        and (p.age65_or_blind_count > count_limits[0] or p.age65_count > count_limits[1])
        for p in profiles
    ):
        notes.append(
            f"A tax profile counts more age 65 / blind boxes than allowed: it is limited to "
            f"{count_limits[0]} boxes and {count_limits[1]} person(s) aged 65 or over"
            + (
                " (each spouse's profile covers only their own boxes)."
                if len(members) > 1 and count_limits[0] == MAX_AGED_BLIND_SINGLE
                else "."
            )
        )
    household_boxes = min(
        sum(boxes for boxes, _ in person_boxes),
        MAX_AGED_BLIND_JOINT if status == "mfj" else MAX_AGED_BLIND_SINGLE,
    )
    household_seniors = min(
        sum(senior for _, senior in person_boxes),
        MAX_SENIORS_JOINT if status == "mfj" else MAX_SENIORS_SINGLE,
        household_boxes,
    )
    inputs = EstimateInputs(
        tax_year=tax_year,
        filing_status=status,
        persons=tuple(_person(f) for f in figures),
        interest_minor=income.get("interest", 0),
        ordinary_dividends_minor=income.get("ordinary_dividends", 0),
        qualified_dividends_minor=income.get("qualified_dividends", 0),
        short_term_gain_minor=income.get("short_term_capital_gain", 0),
        long_term_gain_minor=income.get("long_term_capital_gain", 0),
        other_income_minor=income.get("other", 0),
        profile_other_income_minor=total("other_income_minor"),
        capital_loss_carryover_minor=total("capital_loss_carryover_minor"),
        adjustments_minor=total("adjustments_minor"),
        deduction_mode="itemized" if itemized_any else "standard",
        itemized_deductions_minor=total("itemized_deductions_minor"),
        aged_or_blind_count=household_boxes,
        senior_count=household_seniors,
        nonrefundable_credits_minor=total("nonrefundable_credits_minor"),
        refundable_credits_minor=total("refundable_credits_minor"),
        other_taxes_minor=total("other_taxes_minor"),
        federal_withheld_minor=tax.get("federal_income_tax_withheld", 0),
        estimated_payments_minor=total("estimated_payments_minor"),
        other_payments_minor=total("other_payments_minor"),
        hsa_employer_minor=income.get("hsa_employer", 0),
    )
    calc = compute(inputs, params)
    warnings = [*notes, *calc.warnings]
    if not any(f.has_source for f in figures) and not income:
        warnings.append("No W-2/1099 source forms were imported for this year.")
    for fig in figures:
        if fig.superseded:
            warnings.append(
                f"{len(fig.superseded)} form(s) of user {fig.user_id} were replaced by a "
                "corrected form and are not counted."
            )
            chain = tax_query.correction_warning(fig.superseded)
            if chain is not None:
                warnings.append(chain)

    # the user's own part
    own_fig = figures[own_idx]
    own_person = inputs.persons[own_idx]
    own_addl_medicare_withheld = additional_medicare_withheld(own_person)
    own_payments = (
        own_fig.tax.get("federal_income_tax_withheld", 0)
        + (
            (
                own_profile.estimated_payments_minor
                + own_profile.other_payments_minor
                + own_profile.refundable_credits_minor
            )
            if own_profile
            else 0
        )
        + own_addl_medicare_withheld
        + excess_social_security([own_person], params)
    )
    household = len(members) > 1
    if household:
        bases = [_gross_share_base(f.income, p) for f, p in zip(figures, profiles, strict=True)]
        allocated = _allocate(calc.total_tax_minor, bases)
        weight = sum(bases)
        user_total = allocated[own_idx]
        share = bases[own_idx] / weight if weight > 0 else 1 / len(members)
        user_gross = bases[own_idx]
        warnings.append(
            "Married filing jointly: the household's tax is computed once and allocated to each "
            "spouse by share of gross income. The allocated figure is not a separate liability."
        )
    else:
        user_total = calc.total_tax_minor
        share = None
        user_gross = calc.gross_income_minor

    # cross-check against the user's own return, else the spouse's (a joint return is held by one)
    check_fig = own_fig
    if check_fig.return_figures is None:
        check_fig = next((f for f in figures if f.return_figures is not None), own_fig)
    cross = _cross_check(check_fig, calc, household=household)
    if cross is not None and cross.return_filing_status is not None:
        joint_return = cross.return_filing_status in ("mfj", "qss")
        if joint_return != (status in ("mfj", "qss")):
            warnings.append(
                f"The Form 1040 was filed as {cross.return_filing_status} but this estimate uses "
                f"{status}; the comparison is not like for like."
            )

    resolved = tax_params.resolve()
    return UserEstimate(
        user_id=user_id,
        tax_year=tax_year,
        scope="household_allocation" if household else "individual",
        household_user_ids=tuple(members),
        filing_status_source=status_source,
        calc=calc,
        user_gross_income_minor=user_gross,
        share=share,
        user_total_tax_minor=user_total,
        user_payments_minor=own_payments,
        user_result_minor=own_payments - user_total,
        user_additional_medicare_withheld_minor=own_addl_medicare_withheld,
        cross_check=cross,
        parameters_source=resolved.source,
        parameters_version=resolved.version,
        parameter_sources=params.sources,
        warnings=tuple(dict.fromkeys(warnings)),
    )
