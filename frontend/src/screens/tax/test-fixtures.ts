import type { TaxDocument, TaxIncomePayer, TaxSummary } from "./logic";

export const USERS = [
  { id: 1, name: "Alex", is_default: true },
  { id: 2, name: "Sam", is_default: false },
];

const CATS: Array<[string, string, number, boolean]> = [
  ["salary_wages", "Wages and salary", 10974260, false],
  ["interest", "Interest", 24000, false],
  ["ordinary_dividends", "Ordinary dividends", 290000, false],
  ["qualified_dividends", "Qualified dividends (memo)", 180000, true],
  ["short_term_capital_gain", "Short-term capital gains", 0, false],
  ["long_term_capital_gain", "Long-term capital gains", 0, false],
  ["self_employment", "Self-employment", 0, false],
  ["other", "Other income", 0, false],
];

export function summary(over: Partial<TaxSummary> = {}): TaxSummary {
  return {
    tax_year: 2024,
    user_id: 1,
    income_basis: "source_documents",
    income_by_category: CATS.map(
      ([category, label, amount_minor, is_memo]) => ({
        category:
          category as TaxSummary["income_by_category"][number]["category"],
        label,
        amount_minor,
        is_memo,
      }),
    ),
    memo_by_category: [
      {
        category: "qualified_dividends",
        label: "Qualified dividends (memo)",
        amount_minor: 180000,
      },
      {
        category: "tax_exempt",
        label: "Tax-exempt interest (memo)",
        amount_minor: 5000,
      },
    ],
    total_income_minor: 11288260,
    withheld: [
      {
        category: "federal_income_tax_withheld",
        label: "Federal income tax withheld",
        amount_minor: 1423040,
      },
      {
        category: "state_income_tax_withheld",
        label: "State income tax withheld",
        amount_minor: 500000,
      },
      {
        category: "local_income_tax_withheld",
        label: "Local income tax withheld",
        amount_minor: 0,
      },
    ],
    fica: [
      {
        category: "social_security_tax",
        label: "Social Security tax",
        amount_minor: 700244,
      },
      { category: "medicare_tax", label: "Medicare tax", amount_minor: 163000 },
    ],
    liability: [
      {
        category: "total_federal_tax",
        label: "Total federal tax",
        amount_minor: 1489100,
      },
      {
        category: "federal_tax_ordinary_est",
        label: "Ordinary income tax (estimate)",
        amount_minor: 1400000,
      },
      {
        category: "federal_tax_preferential_est",
        label: "Preferential tax (estimate)",
        amount_minor: 89100,
      },
    ],
    payments_minor: 1519100,
    refund_minor: 30000,
    amount_owed_minor: null,
    refund_or_owed_derived: false,
    federal_effective_rate: 0.1319,
    all_in_rate: 0.2527,
    withholding_rate: 0.1261,
    document_count: 3,
    form_count: 4,
    warnings: ["W-2 wages differ from the 1040 wages line by $12.00."],
    headline: {
      federal_minor: 1489100,
      federal_basis: "return",
      fica_minor: 863244,
      state_minor: 500000,
      total_minor: 1489100 + 863244 + 500000,
    },
    superseded_forms: [],
    ...over,
  };
}

export function document(over: Partial<TaxDocument> = {}): TaxDocument {
  return {
    id: 1,
    user_id: 1,
    original_name: "tax_w2_normal.pdf",
    tax_year: 2024,
    status: "parsed",
    form_types: ["w2"],
    page_count: 1,
    warnings: [],
    error_detail: null,
    created_at: "2026-10-25T12:00:00+00:00",
    has_pdf: false,
    pdf_missing: false,
    pdf_in_place: false,
    staged_until: null,
    ...over,
  };
}

export const PAYERS: TaxIncomePayer[] = [
  {
    tax_year: 2024,
    user_id: 1,
    payer_name: "Pinecrest Logistics LLC",
    form_type: "w2",
    category: "salary_wages",
    label: "Wages and salary",
    amount_minor: 8543210,
  },
  {
    tax_year: 2024,
    user_id: 1,
    payer_name: "Harbor Bank",
    form_type: "1099-int",
    category: "interest",
    label: "Interest",
    amount_minor: 24000,
  },
];

type FormDetail = import("./logic").TaxFormDetail;

export function formDetail(over: Partial<FormDetail> = {}): FormDetail {
  return {
    id: 10,
    form_type: "w2",
    role: "source",
    tax_year: 2024,
    payer_name: "Pinecrest Logistics LLC",
    payer_tin_last4: "1234",
    account_last4: null,
    filing_status: null,
    agi_minor: null,
    taxable_income_minor: null,
    is_corrected: false,
    extraction_method: "parser",
    is_user_edited: false,
    needs_review: false,
    income_items: [
      {
        id: 100,
        category: "salary_wages",
        amount_minor: 8543210,
        currency: "USD",
        source_form: "W-2",
        box: "1",
        label: "Wages",
        is_memo: false,
        origin: "parser",
      },
    ],
    tax_items: [
      {
        id: 200,
        category: "federal_income_tax_withheld",
        amount_minor: 1200000,
        currency: "USD",
        source_form: "W-2",
        box: "2",
        label: null,
        jurisdiction: null,
        is_estimate: false,
        origin: "parser",
      },
    ],
    ...over,
  };
}
