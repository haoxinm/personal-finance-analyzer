import type { components } from "@/api/client";
import { formatBalanceInput, parseBalanceInput } from "@/screens/net-worth/logic";

export type TaxEstimate = components["schemas"]["TaxEstimate"];
export type TaxEstimateCalc = components["schemas"]["TaxEstimateCalc"];
export type TaxEstimateCrossCheck = components["schemas"]["TaxEstimateCrossCheck"];
export type TaxProfile = components["schemas"]["TaxProfile"];
export type TaxProfileIn = components["schemas"]["TaxProfileIn"];
export type EstimateFilingStatus = TaxProfileIn["filing_status"];
export type DeductionMode = TaxProfileIn["deduction_mode"];
type User = components["schemas"]["User"];

export const FILING_STATUS_LABELS: Record<EstimateFilingStatus, string> = {
  single: "Single",
  mfj: "Married filing jointly",
  mfs: "Married filing separately",
  hoh: "Head of household",
  qss: "Qualifying surviving spouse",
};

export function filingStatusLabel(status: string | null | undefined): string {
  if (!status) return "unknown";
  return FILING_STATUS_LABELS[status as EstimateFilingStatus] ?? status;
}

/** Where the filing status of an estimate came from, as a phrase. */
export function filingStatusSourceLabel(
  source: TaxEstimate["filing_status_source"],
): string {
  switch (source) {
    case "profile":
      return "from your profile";
    case "return":
      return "from your Form 1040";
    case "default":
      return "assumed: no profile or return";
  }
}

// ---- Result wording ---------------------------------------------------------------------------

export type ResultKind = "refund" | "owe" | "even";

/** `result = payments - tax`: positive is a refund, negative an amount owed. */
export function resultKind(resultMinor: number): ResultKind {
  return resultMinor > 0 ? "refund" : resultMinor < 0 ? "owe" : "even";
}

export function resultWord(kind: ResultKind): string {
  return kind === "refund" ? "Refund" : kind === "owe" ? "Owe" : "Even";
}

/** "Alex" for "user 1" in a server message (warnings and conflict texts name users by id). */
export function withUserNames(text: string, users: readonly User[]): string {
  return text.replace(/\buser (\d+)\b/gi, (match, id: string) => {
    const user = users.find((u) => u.id === Number(id));
    return user ? user.name : match;
  });
}

// ---- Profile form -----------------------------------------------------------------------------

export interface MoneyFieldSpec {
  key: MoneyKey;
  label: string;
  help: string;
}

export type MoneyKey = Exclude<
  Extract<keyof TaxProfileIn, `${string}_minor`>,
  "itemized_deductions_minor"
>;

/** The profile's dollar fields in form order (itemized deductions are shown with the deduction mode). */
export const MONEY_FIELDS: readonly MoneyFieldSpec[] = [
  {
    key: "adjustments_minor",
    label: "Adjustments to income",
    help: "Above-the-line deductions you took outside payroll: traditional IRA, student loan interest, HSA contributions you made yourself.",
  },
  {
    key: "other_income_minor",
    label: "Other income",
    help: "Taxable amount of income no form here covers: Social Security benefits, IRA or pension payouts, rental or partnership income.",
  },
  {
    key: "capital_loss_carryover_minor",
    label: "Capital loss carryover",
    help: "Unused capital loss carried in from last year's return (Schedule D).",
  },
  {
    key: "nonrefundable_credits_minor",
    label: "Nonrefundable credits",
    help: "Total of credits that can only reduce income tax to zero, such as the child tax credit or education credits.",
  },
  {
    key: "refundable_credits_minor",
    label: "Refundable credits",
    help: "Total of credits paid out even beyond your tax, such as the earned income credit.",
  },
  {
    key: "other_taxes_minor",
    label: "Other taxes",
    help: "Taxes outside income tax and self-employment tax, such as household employment tax.",
  },
  {
    key: "estimated_payments_minor",
    label: "Estimated tax payments",
    help: "Quarterly payments you made for this year.",
  },
  {
    key: "other_payments_minor",
    label: "Other payments",
    help: "Other federal payments toward this year, such as an extension payment.",
  },
];

export interface ProfileDraft {
  filingStatus: EstimateFilingStatus;
  /** The spouse (another user), only meaningful with married filing jointly. */
  spouseId: number | null;
  deductionMode: DeductionMode;
  /** The itemized total, as typed. */
  itemized: string;
  age65OrBlind: number;
  /** The other dollar fields, as typed (blank = 0). */
  money: Record<MoneyKey, string>;
}

const PLAIN_KEYS: readonly MoneyKey[] = MONEY_FIELDS.map((f) => f.key);

function toInput(minor: number): string {
  return minor === 0 ? "" : formatBalanceInput(minor);
}

/** The form's starting values: the saved profile, else what the estimate assumed. */
export function draftFromProfile(
  profile: TaxProfile | null,
  fallback: { filingStatus: EstimateFilingStatus; spouseId: number | null },
): ProfileDraft {
  const money = Object.fromEntries(
    PLAIN_KEYS.map((k) => [k, toInput(profile?.[k] ?? 0)]),
  ) as ProfileDraft["money"];
  return {
    filingStatus: profile?.filing_status ?? fallback.filingStatus,
    spouseId: profile ? (profile.joint_with_user_id ?? null) : fallback.spouseId,
    deductionMode: profile?.deduction_mode ?? "standard",
    itemized: toInput(profile?.itemized_deductions_minor ?? 0),
    age65OrBlind: profile?.age65_or_blind_count ?? 0,
    money,
  };
}

export const MONEY_ERROR = "Enter a dollar amount of 0 or more";

/** Dollars as typed to cents: blank is 0, anything unreadable or negative is `null`. */
export function parseMoneyField(raw: string): number | null {
  return raw.trim() === "" ? 0 : parseBalanceInput(raw);
}

export type DraftErrors = Partial<Record<MoneyKey | "itemized", string>>;

/** The `PUT` body for a draft (cents), or a message per unreadable field. */
export function draftToBody(
  draft: ProfileDraft,
): { body: TaxProfileIn } | { errors: DraftErrors } {
  const errors: DraftErrors = {};
  const itemized = parseMoneyField(draft.itemized);
  if (itemized === null) errors.itemized = MONEY_ERROR;
  const money = {} as Record<MoneyKey, number>;
  for (const key of PLAIN_KEYS) {
    const value = parseMoneyField(draft.money[key]);
    if (value === null) errors[key] = MONEY_ERROR;
    else money[key] = value;
  }
  if (Object.keys(errors).length > 0) return { errors };
  return {
    body: {
      filing_status: draft.filingStatus,
      joint_with_user_id: draft.filingStatus === "mfj" ? draft.spouseId : null,
      deduction_mode: draft.deductionMode,
      itemized_deductions_minor: itemized ?? 0,
      age65_or_blind_count: draft.age65OrBlind,
      ...money,
    },
  };
}

/** Whether two drafts mean the same profile ("100" and "100.00" do). */
export function sameDraft(a: ProfileDraft, b: ProfileDraft): boolean {
  const norm = (d: ProfileDraft) =>
    JSON.stringify([
      d.filingStatus,
      d.filingStatus === "mfj" ? d.spouseId : null,
      d.deductionMode,
      parseMoneyField(d.itemized) ?? `raw:${d.itemized}`,
      d.age65OrBlind,
      PLAIN_KEYS.map((k) => parseMoneyField(d.money[k]) ?? `raw:${d.money[k]}`),
    ]);
  return norm(a) === norm(b);
}

// ---- Breakdown rows ---------------------------------------------------------------------------

/** Where a line of the computation comes from. */
export type LineSource = "documents" | "profile" | "computed";

export const LINE_SOURCE_LABELS: Record<LineSource, string> = {
  documents: "Documents",
  profile: "Profile",
  computed: "Computed",
};

export interface BreakdownLine {
  key: string;
  label: string;
  minor: number;
  source?: LineSource;
  /** `line` adds to or subtracts from the running total; `subtotal` and `total` are the results;
   * `memo` is shown for information and is not added again. */
  kind: "line" | "subtotal" | "total" | "memo";
  /** Drawn as a subtraction (the amount itself is stored positive). */
  minus?: boolean;
}

export interface BreakdownSection {
  key: string;
  title: string;
  lines: BreakdownLine[];
}

/** The computation, top to bottom, with where each input came from. Optional lines that are zero
 * are left out; the lines that always matter (wages, AGI, taxable income, total tax) stay. */
export function breakdownSections(c: TaxEstimateCalc): BreakdownSection[] {
  const line = (
    key: string,
    label: string,
    minor: number,
    source: LineSource,
    opts: { minus?: boolean; always?: boolean; kind?: BreakdownLine["kind"] } = {},
  ): BreakdownLine[] =>
    minor === 0 && !opts.always
      ? []
      : [{ key, label, minor, source, kind: opts.kind ?? "line", minus: opts.minus }];
  const total = (
    key: string,
    label: string,
    minor: number,
    kind: "subtotal" | "total" = "subtotal",
  ): BreakdownLine[] => [{ key, label, minor, kind }];
  const memo = (
    key: string,
    label: string,
    minor: number,
    source?: LineSource,
  ): BreakdownLine[] =>
    minor === 0 ? [] : [{ key, label, minor, source, kind: "memo" }];

  const capitalInvolved =
    c.short_term_gain_minor !== 0 ||
    c.long_term_gain_minor !== 0 ||
    c.capital_loss_carryover_in_minor !== 0;
  const itemized = c.deduction_mode === "itemized";

  return [
    {
      key: "income",
      title: "Income",
      lines: [
        ...line("wages", "Wages (W-2 box 1)", c.wages_minor, "documents", { always: true }),
        ...line("interest", "Interest", c.interest_minor, "documents"),
        ...line("dividends", "Ordinary dividends", c.ordinary_dividends_minor, "documents"),
        ...memo("qualified", "of which qualified dividends", c.qualified_dividends_minor),
        ...(capitalInvolved
          ? [
              ...line(
                "capital",
                "Capital gains, net of losses",
                c.capital_result_counted_minor,
                "computed",
                { always: true },
              ),
              ...memo("stg", "short-term gain or loss", c.short_term_gain_minor, "documents"),
              ...memo("ltg", "long-term gain or loss", c.long_term_gain_minor, "documents"),
              ...memo(
                "carry-in",
                "loss carried in from last year",
                c.capital_loss_carryover_in_minor,
                "profile",
              ),
              ...memo(
                "carry-out",
                "loss carried to next year (over the annual limit)",
                c.capital_loss_carryover_out_minor,
                "computed",
              ),
            ]
          : []),
        ...line("se", "Self-employment income", c.self_employment_minor, "documents"),
        ...line("other-doc", "Other income", c.other_income_minor, "documents"),
        ...line("other-profile", "Other income you entered", c.profile_other_income_minor, "profile"),
        ...total("gross", "Gross income", c.gross_income_minor),
      ],
    },
    {
      key: "agi",
      title: "Adjusted gross income",
      lines: [
        ...line("adjustments", "Adjustments to income", c.adjustments_minor, "profile", { minus: true }),
        ...line("se-deduction", "Half of self-employment tax", c.se_tax_deduction_minor, "computed", {
          minus: true,
        }),
        ...total("agi", "Adjusted gross income (AGI)", c.agi_minor),
      ],
    },
    {
      key: "taxable",
      title: "Taxable income",
      lines: [
        ...line(
          "deduction",
          itemized ? "Itemized deductions" : "Standard deduction",
          c.deduction_minor,
          itemized ? "profile" : "computed",
          { minus: true, always: true },
        ),
        ...total("taxable", "Taxable income", c.taxable_income_minor),
        ...memo(
          "preferential",
          "of which qualified dividends and long-term gains",
          c.preferential_income_minor,
        ),
      ],
    },
    {
      key: "tax",
      title: "Tax",
      lines: [
        ...line("ordinary-tax", "Tax on ordinary income", c.ordinary_tax_minor, "computed", { always: true }),
        ...line(
          "preferential-tax",
          "Tax on dividends and long-term gains",
          c.preferential_tax_minor,
          "computed",
        ),
        ...total("income-tax", "Income tax before credits", c.income_tax_minor),
        ...line("nonrefundable", "Nonrefundable credits", c.nonrefundable_credits_minor, "profile", {
          minus: true,
        }),
        ...line("se-tax", "Self-employment tax", c.se_tax_minor, "computed"),
        ...line("other-taxes", "Other taxes", c.other_taxes_minor, "profile"),
        ...line("refundable", "Refundable credits", c.refundable_credits_minor, "profile", {
          minus: true,
        }),
        ...total("total-tax", "Total tax", c.total_tax_minor, "total"),
      ],
    },
    {
      key: "payments",
      title: "Payments",
      lines: [
        ...line("withheld", "Federal tax withheld", c.federal_withheld_minor, "documents", { always: true }),
        ...line("estimated", "Estimated tax payments", c.estimated_payments_minor, "profile"),
        ...line("other-payments", "Other payments", c.other_payments_minor, "profile"),
        ...total("payments", "Total payments", c.total_payments_minor, "total"),
      ],
    },
  ];
}
