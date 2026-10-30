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

/**
 * The years the Estimate tab offers, newest first: every year with an imported form, plus the
 * current and previous calendar year so a profile-only estimate can start before any form is
 * imported (there is no endpoint listing saved profiles, so those years cannot be discovered).
 */
export function estimateYearOptions(documentYears: readonly number[], now: Date = new Date()): number[] {
  const current = now.getFullYear();
  return [...new Set([current, current - 1, ...documentYears])].sort((a, b) => b - a);
}

/** The year selected when `?year=` names none: the newest imported one, else the filing year just past. */
export function defaultEstimateYear(documentYears: readonly number[], now: Date = new Date()): number {
  return documentYears.length > 0 ? Math.max(...documentYears) : now.getFullYear() - 1;
}

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
    help: "The part of your credits that can only reduce income tax to zero: the child tax credit or credit for other dependents (CTC/ODC), education credits, and so on. The estimate uses at most your income tax, so the excess is not refunded. Enter the refundable part (ACTC, EITC, refundable AOTC) under refundable credits instead.",
  },
  {
    key: "refundable_credits_minor",
    label: "Refundable credits",
    help: "Refundable credits (treated like payments): the part of your credits paid out even beyond your tax, such as the additional child tax credit (ACTC), the earned income credit (EITC) and the refundable part of the American opportunity credit.",
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
  /** Boxes checked for the additional standard deduction (age 65 or older, blind): up to 2 per
   * person, 4 on a joint profile. */
  age65OrBlind: number;
  /** People aged 65 or older (the 2025-2028 senior deduction): 0 or 1, up to 2 on a joint profile. */
  age65: number;
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
  // A saved profile may hold more than its filing status allows (an old household total).
  return clampAge({
    filingStatus: profile?.filing_status ?? fallback.filingStatus,
    spouseId: profile ? (profile.joint_with_user_id ?? null) : fallback.spouseId,
    deductionMode: profile?.deduction_mode ?? "standard",
    itemized: toInput(profile?.itemized_deductions_minor ?? 0),
    age65OrBlind: profile?.age65_or_blind_count ?? 0,
    age65: profile?.age65_count ?? 0,
    money,
  });
}

export const MONEY_ERROR = "Enter a dollar amount of 0 or more";

/** What the API accepts: the boxes checked for the additional standard deduction (age 65 or older,
 * blind) and the "65 or older" count, which is never more than the boxes. A profile describes one
 * person: at most 2 boxes and 1 person aged 65+. A joint profile may hold both spouses' (4 and 2),
 * for a spouse who has no profile of their own in the app; when both spouses have a profile the
 * estimate limits each to 2 and 1 and adds them together. */
export const AGE_LIMITS = {
  person: { boxes: 2, age65: 1 },
  joint: { boxes: 4, age65: 2 },
} as const;

export interface AgeLimits {
  readonly boxes: number;
  readonly age65: number;
}

/** The limits for a filing status (the API's: 4 and 2 for `mfj`, 2 and 1 for every other). */
export function ageLimits(filingStatus: EstimateFilingStatus): AgeLimits {
  return filingStatus === "mfj" ? AGE_LIMITS.joint : AGE_LIMITS.person;
}

/** The draft's age counts brought back within the limits of its filing status (the API refuses
 * more: switching from joint to another status, or an older profile, may hold too many). */
export function clampAge(draft: ProfileDraft): ProfileDraft {
  const limits = ageLimits(draft.filingStatus);
  const age65OrBlind = Math.min(draft.age65OrBlind, limits.boxes);
  const age65 = Math.min(draft.age65, limits.age65, age65OrBlind);
  return age65OrBlind === draft.age65OrBlind && age65 === draft.age65
    ? draft
    : { ...draft, age65OrBlind, age65 };
}

/** Dollars as typed to cents: blank is 0, anything unreadable or negative is `null`. */
export function parseMoneyField(raw: string): number | null {
  return raw.trim() === "" ? 0 : parseBalanceInput(raw);
}

export type DraftErrors = Partial<Record<MoneyKey | "itemized" | "age65OrBlind" | "age65", string>>;

/** The `PUT` body for a draft (cents), or a message per unreadable field. */
export function draftToBody(
  draft: ProfileDraft,
): { body: TaxProfileIn } | { errors: DraftErrors } {
  const errors: DraftErrors = {};
  const itemized = parseMoneyField(draft.itemized);
  if (itemized === null) errors.itemized = MONEY_ERROR;
  const limits = ageLimits(draft.filingStatus);
  const scope = draft.filingStatus === "mfj" ? "for a joint profile" : "per person";
  if (draft.age65OrBlind > limits.boxes) {
    errors.age65OrBlind = `At most ${limits.boxes} ${scope}`;
  }
  if (draft.age65 > limits.age65) {
    errors.age65 = `At most ${limits.age65} ${scope}`;
  } else if (draft.age65 > draft.age65OrBlind) {
    errors.age65 = "Cannot be more than the boxes checked (it is a subset)";
  }
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
      age65_count: draft.age65,
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
      d.age65,
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
        ...line("senior-deduction", "Senior deduction (2025-2028)", c.senior_deduction_minor, "computed", {
          minus: true,
        }),
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
        // The credits actually used: capped at the income tax (the server's `nonrefundable_credits_minor`).
        ...line("nonrefundable", "Nonrefundable credits used", c.nonrefundable_credits_minor, "profile", {
          minus: true,
        }),
        ...(c.nonrefundable_credits_entered_minor > c.nonrefundable_credits_minor
          ? memo(
              "nonrefundable-entered",
              "entered, more than the income tax: the excess is not refunded",
              c.nonrefundable_credits_entered_minor,
              "profile",
            )
          : []),
        ...line("se-tax", "Self-employment tax", c.se_tax_minor, "computed"),
        ...line("niit", "Net investment income tax", c.net_investment_income_tax_minor, "computed"),
        ...line("additional-medicare", "Additional Medicare Tax", c.additional_medicare_tax_minor, "computed"),
        ...line("other-taxes", "Other taxes", c.other_taxes_minor, "profile"),
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
        // Refundable credits are payments (1040 lines 27-31), never negative tax.
        ...line("refundable", "Refundable credits", c.refundable_credits_minor, "profile"),
        ...line(
          "additional-medicare-withheld",
          "Additional Medicare Tax withheld",
          c.additional_medicare_withheld_minor,
          "documents",
        ),
        ...line(
          "excess-ss",
          "Excess Social Security tax credit",
          c.excess_social_security_credit_minor,
          "computed",
        ),
        ...total("payments", "Total payments", c.total_payments_minor, "total"),
      ],
    },
  ];
}
