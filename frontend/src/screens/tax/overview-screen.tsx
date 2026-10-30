import { Link } from "react-router-dom";

import {
  EmptyState,
  ErrorState,
  LoadingState,
} from "@/components/primitives/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useSelectedUser } from "@/hooks/use-selected-user";

import { BasisBanner } from "./components/basis-banner";
import { IncomeBreakdown } from "./components/income-breakdown";
import { IncomeByPayer } from "./components/income-by-payer";
import { SummaryCards } from "./components/summary-cards";
import { TaxBreakdown } from "./components/tax-breakdown";
import { YearOverYear } from "./components/year-over-year";
import { YearPicker } from "./components/year-picker";
import {
  useTaxIncomeByPayer,
  useTaxSummary,
  useTaxYear,
  useTaxYears,
} from "./hooks";

export const screenMeta = { path: "/tax", title: "Overview" };

const EMPTY_TEXT = "Import a W-2, 1099 or 1040 to see income and tax";

/**
 * Income & Tax overview: per-year income by category and payer, withholding / FICA / liability,
 * effective rates and the year-over-year trend. Amounts are neutral (income and tax are not
 * "spending"). The documents behind the numbers live on the Documents tab.
 */
export default function TaxOverviewScreen() {
  const sel = useSelectedUser();
  const ready = !sel.isLoading;
  const yearsQuery = useTaxYears(sel.userIds, ready);
  const summaryQuery = useTaxSummary(sel.userIds, ready);
  const payersQuery = useTaxIncomeByPayer(sel.userIds, ready);

  const years = yearsQuery.data ?? [];
  const [year, setYear] = useTaxYear(years);
  const summaries = summaryQuery.data ?? [];
  const summary = summaries.find((s) => s.tax_year === year) ?? null;

  const error = yearsQuery.error ?? summaryQuery.error;
  const loading =
    sel.isLoading || yearsQuery.isLoading || summaryQuery.isLoading;
  const hasData = years.length > 0 && summaries.length > 0;

  return (
    <div className="flex flex-col gap-6 py-2">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">
            Income &amp; Tax
          </h1>
          <p className="text-sm text-muted-foreground">
            Income, withholding and tax from your W-2, 1099 and 1040 forms.
            {sel.users.length > 1 && sel.selectedUser
              ? ` Showing ${sel.selectedUser.name}.`
              : ""}
          </p>
        </div>
      </div>

      {error ? (
        <ErrorState
          description={error instanceof Error ? error.message : String(error)}
          onRetry={() => {
            void yearsQuery.refetch();
            void summaryQuery.refetch();
          }}
        />
      ) : loading ? (
        <LoadingState rows={4} />
      ) : !hasData ? (
        <EmptyState
          title={EMPTY_TEXT}
          description="Upload tax form PDFs to get started."
          action={
            <Button asChild size="sm" variant="outline">
              <Link to="/tax/import">Import tax forms</Link>
            </Button>
          }
        />
      ) : (
        <>
          <YearPicker years={years} value={year} onChange={setYear} />
          {summary ? (
            <>
              <BasisBanner summary={summary} />
              <SummaryCards summary={summary} />
            </>
          ) : null}
          {summaries.length > 1 ? (
            <Card>
              <CardHeader>
                <CardTitle>Year over year</CardTitle>
              </CardHeader>
              <CardContent>
                <YearOverYear summaries={summaries} />
              </CardContent>
            </Card>
          ) : null}
          {summary ? (
            <>
              <div className="grid items-start gap-4 lg:grid-cols-2">
                <Card>
                  <CardHeader>
                    <CardTitle>Income by category</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <IncomeBreakdown summary={summary} />
                  </CardContent>
                </Card>
                <Card>
                  <CardHeader>
                    <CardTitle>Tax breakdown</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <TaxBreakdown summary={summary} />
                  </CardContent>
                </Card>
              </div>
              <Card>
                <CardHeader>
                  <CardTitle>Income by payer</CardTitle>
                </CardHeader>
                <CardContent>
                  {payersQuery.error ? (
                    <ErrorState
                      description={payersQuery.error.message}
                      onRetry={() => void payersQuery.refetch()}
                    />
                  ) : (
                    <IncomeByPayer
                      rows={payersQuery.data ?? []}
                      year={summary.tax_year}
                    />
                  )}
                </CardContent>
              </Card>
            </>
          ) : null}
        </>
      )}
    </div>
  );
}
