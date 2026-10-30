import type { TaxEstimate, TaxEstimateCalc, TaxProfile } from "./logic";

/** A single filer owing a little: $100,000 of wages, some interest and dividends, standard deduction. */
export function estimateCalc(over: Partial<TaxEstimateCalc> = {}): TaxEstimateCalc {
  return {
    filing_status: "single",
    wages_minor: 10000000,
    interest_minor: 24000,
    ordinary_dividends_minor: 290000,
    qualified_dividends_minor: 180000,
    short_term_gain_minor: 0,
    long_term_gain_minor: 0,
    capital_loss_carryover_in_minor: 0,
    capital_result_counted_minor: 0,
    capital_loss_carryover_out_minor: 0,
    self_employment_minor: 0,
    other_income_minor: 0,
    profile_other_income_minor: 0,
    gross_income_minor: 10314000,
    adjustments_minor: 0,
    se_tax_deduction_minor: 0,
    agi_minor: 10314000,
    deduction_mode: "standard",
    deduction_minor: 1460000,
    taxable_income_minor: 8854000,
    preferential_income_minor: 180000,
    income_tax_minor: 1600000,
    ordinary_tax_minor: 1573000,
    preferential_tax_minor: 27000,
    nonrefundable_credits_minor: 0,
    se_tax_minor: 0,
    other_taxes_minor: 0,
    refundable_credits_minor: 0,
    total_tax_minor: 1600000,
    federal_withheld_minor: 1423040,
    estimated_payments_minor: 0,
    other_payments_minor: 0,
    total_payments_minor: 1423040,
    result_minor: -176960,
    refund_minor: 0,
    owed_minor: 176960,
    effective_rate: 0.1551,
    marginal_rate: 0.24,
    preferential_rate: 0.15,
    retirement_deferral_minor: 0,
    hsa_employer_minor: 0,
    ...over,
  };
}

export function taxProfile(over: Partial<TaxProfile> = {}): TaxProfile {
  return {
    user_id: 1,
    tax_year: 2024,
    updated_at: "2026-10-25T12:00:00+00:00",
    filing_status: "single",
    joint_with_user_id: null,
    deduction_mode: "standard",
    itemized_deductions_minor: 0,
    age65_or_blind_count: 0,
    adjustments_minor: 0,
    other_income_minor: 0,
    capital_loss_carryover_minor: 0,
    nonrefundable_credits_minor: 0,
    refundable_credits_minor: 0,
    other_taxes_minor: 0,
    estimated_payments_minor: 0,
    other_payments_minor: 0,
    ...over,
  };
}

export const NOT_MODELED = [
  "alternative minimum tax",
  "net investment income tax and Additional Medicare Tax",
];

/** An individual estimate (no profile, no return) that owes $1,769.60. */
export function estimate(over: Partial<TaxEstimate> = {}): TaxEstimate {
  const calc = over.calc ?? estimateCalc();
  return {
    user_id: 1,
    tax_year: 2024,
    scope: "individual",
    household_user_ids: [1],
    filing_status: calc.filing_status,
    filing_status_source: "default",
    calc,
    user: {
      gross_income_minor: calc.gross_income_minor,
      share: null,
      total_tax_minor: calc.total_tax_minor,
      payments_minor: calc.total_payments_minor,
      result_minor: calc.result_minor,
      refund_minor: calc.refund_minor,
      owed_minor: calc.owed_minor,
    },
    cross_check: null,
    profile: null,
    parameters: {
      tax_year: 2024,
      source: "bundled",
      params_version: 3,
      sources: ["Rev. Proc. 2023-34", "SSA 2024 contribution and benefit base"],
    },
    warnings: ["No tax profile: filing status single and the standard deduction are assumed."],
    not_modeled: NOT_MODELED,
    ...over,
  };
}

/** A refund of $500.00: withheld more than the tax. */
export const REFUND_CALC = estimateCalc({
  total_tax_minor: 1000000,
  income_tax_minor: 1000000,
  federal_withheld_minor: 1050000,
  total_payments_minor: 1050000,
  result_minor: 50000,
  refund_minor: 50000,
  owed_minor: 0,
});

/** Alex and Sam, married filing jointly: household calc, Alex's allocated 60% share. */
export function householdEstimate(over: Partial<TaxEstimate> = {}): TaxEstimate {
  const calc = estimateCalc({
    filing_status: "mfj",
    gross_income_minor: 10000000,
    total_tax_minor: 1600000,
    total_payments_minor: 1700000,
    federal_withheld_minor: 1700000,
    result_minor: 100000,
    refund_minor: 100000,
    owed_minor: 0,
  });
  return estimate({
    scope: "household_allocation",
    household_user_ids: [1, 2],
    filing_status: "mfj",
    filing_status_source: "profile",
    calc,
    user: {
      gross_income_minor: 6000000,
      share: 0.6,
      total_tax_minor: 960000,
      payments_minor: 1000000,
      result_minor: 40000,
      refund_minor: 40000,
      owed_minor: 0,
    },
    warnings: [
      "Married filing jointly: the household's tax is computed once and allocated to each spouse by share of gross income. The allocated figure is not a separate liability.",
    ],
    ...over,
  });
}
