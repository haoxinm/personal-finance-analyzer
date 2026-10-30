import { Money } from "@/components/primitives/money";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

import { formatRate } from "../logic";
import { ResultAmount } from "./display";
import type { TaxEstimate } from "./logic";

function StatCard({
  label,
  hint,
  title,
  testId,
  children,
}: {
  label: string;
  hint?: React.ReactNode;
  title?: string;
  testId?: string;
  children: React.ReactNode;
}) {
  return (
    <Card data-testid={testId} title={title}>
      <CardContent className="flex flex-col gap-1 p-4">
        <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</span>
        <span className="text-2xl font-semibold tabular-nums">{children}</span>
        {hint ? <span className="text-xs text-muted-foreground">{hint}</span> : null}
      </CardContent>
    </Card>
  );
}

/**
 * Headline cards: the result, total tax and the two rates. For a joint return the first card is this
 * person's allocated result, the tax card is the household's, and both say so.
 */
export function EstimateCards({ estimate }: { estimate: TaxEstimate }) {
  const { calc, user } = estimate;
  const household = estimate.scope === "household_allocation";
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4" data-testid="estimate-cards">
      <StatCard
        label={household ? "Your result (allocated)" : "Estimated result"}
        testId="estimate-result-card"
        hint={
          household
            ? "Your share of the household tax, against your own payments"
            : "Payments minus total tax"
        }
      >
        <ResultAmount resultMinor={household ? user.result_minor : calc.result_minor} />
      </StatCard>
      <StatCard
        label={household ? "Household total tax" : "Total tax"}
        testId="estimate-tax-card"
        hint={
          <>
            Income tax <Money tone="neutral" minorUnits={calc.income_tax_minor} /> · Self-employment{" "}
            <Money tone="neutral" minorUnits={calc.se_tax_minor} />
          </>
        }
      >
        <Money tone="neutral" minorUnits={calc.total_tax_minor} />
      </StatCard>
      <StatCard
        label="Effective rate"
        testId="estimate-effective-card"
        title="Total tax divided by gross income."
        hint={household ? "Household tax ÷ household gross income" : "Total tax ÷ gross income"}
      >
        {formatRate(calc.effective_rate)}
      </StatCard>
      <StatCard
        label="Marginal rate"
        testId="estimate-marginal-card"
        title="The ordinary-income bracket the next dollar of income falls in."
        hint={
          calc.preferential_rate !== null
            ? `Dividends and long-term gains: ${formatRate(calc.preferential_rate)}`
            : "Bracket of the next dollar of income"
        }
      >
        {formatRate(calc.marginal_rate)}
      </StatCard>
    </div>
  );
}

/** A joint return's household figures cannot be split exactly: this person's part is the household
 * tax allocated by share of gross income, against their own payments. */
export function AllocationCard({
  estimate,
  names,
}: {
  estimate: TaxEstimate;
  names: string;
}) {
  const { user } = estimate;
  const share = user.share === null ? null : formatRate(user.share).replace(/\.00%$/, "%");
  return (
    <Card data-testid="allocation-card">
      <CardHeader>
        <CardTitle>Your share of the joint return</CardTitle>
        <p className="text-xs text-muted-foreground">
          Allocated by share of income: the household tax of {names} is split by each person&apos;s share of
          gross income. It is an allocation, not a separate liability.
        </p>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1 text-sm">
          <dt>Your gross income</dt>
          <dd className="text-right tabular-nums">
            <Money tone="neutral" minorUnits={user.gross_income_minor} />
          </dd>
          <dt>Your share</dt>
          <dd className="text-right tabular-nums" data-testid="allocation-share">
            {share ?? "—"}
          </dd>
          <dt>Your allocated tax</dt>
          <dd className="text-right tabular-nums">
            <Money tone="neutral" minorUnits={user.total_tax_minor} />
          </dd>
          <dt>Your payments (withholding, estimated, other)</dt>
          <dd className="text-right tabular-nums">
            <Money tone="neutral" minorUnits={user.payments_minor} />
          </dd>
          <dt className="border-t pt-1 font-semibold">Your result</dt>
          <dd className="border-t pt-1 text-right font-semibold tabular-nums" data-testid="allocation-result">
            <ResultAmount resultMinor={user.result_minor} />
          </dd>
        </dl>
      </CardContent>
    </Card>
  );
}
