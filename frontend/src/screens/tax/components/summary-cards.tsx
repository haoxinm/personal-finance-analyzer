import { Link } from "react-router-dom";

import { Money } from "@/components/primitives/money";
import { Card, CardContent } from "@/components/ui/card";

import {
  formatRate,
  headlineBasisLabel,
  headlineBasisNote,
  headlineTax,
  refundOrOwed,
  type TaxSummary,
} from "../logic";

function StatCard({
  label,
  hint,
  title,
  children,
  testId,
}: {
  label: string;
  hint?: React.ReactNode;
  title?: string;
  children: React.ReactNode;
  testId?: string;
}) {
  return (
    <Card data-testid={testId} title={title}>
      <CardContent className="flex flex-col gap-1 p-4">
        <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {label}
        </span>
        <span className="text-2xl font-semibold tabular-nums">{children}</span>
        {hint ? (
          <span className="text-xs text-muted-foreground">{hint}</span>
        ) : null}
      </CardContent>
    </Card>
  );
}

/** Link to the Estimate tab for the summary's year. */
function EstimateLink({ year }: { year: number }) {
  return (
    <Link
      to={`/tax/estimate?year=${year}`}
      className="font-medium text-foreground underline-offset-2 hover:underline"
    >
      See estimate →
    </Link>
  );
}

export function SummaryCards({ summary }: { summary: TaxSummary }) {
  const tax = headlineTax(summary);
  const result = refundOrOwed(summary);
  const resultLabel =
    result.kind === "owed"
      ? "Amount owed"
      : result.kind === "refund"
        ? "Refund"
        : "Refund / owed";
  return (
    <div
      className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5"
      data-testid="tax-summary-cards"
    >
      <StatCard
        label="Total income"
        hint={`${summary.document_count} document${summary.document_count === 1 ? "" : "s"}`}
      >
        <Money tone="neutral" minorUnits={summary.total_income_minor} />
      </StatCard>
      <StatCard
        label="Total tax"
        testId="total-tax-card"
        hint={
          tax ? (
            <>
              Federal <Money tone="neutral" minorUnits={tax.federalMinor} />{" "}
              <span
                data-testid="federal-basis"
                title={headlineBasisNote(tax.federalBasis)}
              >
                ({headlineBasisLabel(tax.federalBasis)})
              </span>{" "}
              · FICA <Money tone="neutral" minorUnits={tax.ficaMinor} /> · State{" "}
              <Money tone="neutral" minorUnits={tax.stateMinor} />
              {" · "}
              <EstimateLink year={summary.tax_year} />
            </>
          ) : (
            <>
              No tax figures for this year · <EstimateLink year={summary.tax_year} />
            </>
          )
        }
      >
        {tax ? <Money tone="neutral" minorUnits={tax.totalMinor} /> : "—"}
      </StatCard>
      <StatCard
        label="Effective federal rate"
        title="Total federal tax from your Form 1040 divided by total income. It needs a return: without one it shows a dash (the estimate has its own rate)."
        hint={
          summary.federal_effective_rate === null ? (
            <>
              Needs a Form 1040 · <EstimateLink year={summary.tax_year} />
            </>
          ) : (
            "Return's federal tax ÷ total income"
          )
        }
        testId="effective-rate-card"
      >
        {formatRate(summary.federal_effective_rate)}
      </StatCard>
      <StatCard
        label="All-in rate"
        title="Federal, FICA and state tax combined, divided by total income. Federal is the return's tax when there is one, otherwise what was withheld."
        hint="Federal + FICA + state ÷ income"
        testId="all-in-rate-card"
      >
        {formatRate(summary.all_in_rate)}
      </StatCard>
      <StatCard
        label={resultLabel}
        hint={
          result.kind === "unknown" ? (
            <>
              Import your 1040, or <EstimateLink year={summary.tax_year} />
            </>
          ) : result.derived ? (
            "Estimated: payments minus tax"
          ) : result.kind === "even" ? (
            "Nothing due or returned"
          ) : (
            "From your return"
          )
        }
      >
        {result.amountMinor === null ? (
          "—"
        ) : (
          <Money tone="neutral" minorUnits={result.amountMinor} />
        )}
      </StatCard>
    </div>
  );
}
