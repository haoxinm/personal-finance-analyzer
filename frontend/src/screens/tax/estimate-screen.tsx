import * as React from "react";
import { Link } from "react-router-dom";

import { Page, PageHeader } from "@/components/primitives/page";
import { ErrorState, LoadingState } from "@/components/primitives/states";
import { useSelectedUser } from "@/hooks/use-selected-user";

import { YearPicker } from "./components/year-picker";
import { EstimateView } from "./estimate/estimate-view";
import { defaultEstimateYear, estimateYearOptions } from "./estimate/logic";
import { PeopleOverview } from "./estimate/people-overview";
import { useTaxYear, useTaxYears } from "./hooks";

export const screenMeta = { path: "/tax/estimate", title: "Estimate" };

/**
 * Income & tax estimate: a simple federal income tax estimate for one person and year, computed from
 * the imported source forms and a few manual inputs (filing status, deduction, credits, payments).
 * The Form 1040, when there is one, is only a cross-check. With "All users" selected the screen shows
 * one summary card per person, because the estimate needs a person. The year picker offers every year
 * with an imported form plus the current and previous year, so an estimate can start from the manual
 * inputs alone.
 */
export default function TaxEstimateScreen() {
  const sel = useSelectedUser();
  const yearsQuery = useTaxYears(sel.userIds, !sel.isLoading);
  const documentYears = React.useMemo(() => yearsQuery.data ?? [], [yearsQuery.data]);
  const years = React.useMemo(() => estimateYearOptions(documentYears), [documentYears]);
  const [year, setYear] = useTaxYear(years, defaultEstimateYear(documentYears));

  const loading = sel.isLoading || yearsQuery.isLoading;
  const error = yearsQuery.error;

  return (
    <Page>
      <PageHeader
        title="Tax estimate"
        description="A planning estimate of federal income tax from your source forms and a few inputs. Not tax advice and not a substitute for your return."
      />

      {error ? (
        <ErrorState
          description={error instanceof Error ? error.message : String(error)}
          onRetry={() => void yearsQuery.refetch()}
        />
      ) : loading ? (
        <LoadingState rows={4} />
      ) : year === null ? null : (
        <>
          <YearPicker years={years} value={year} onChange={setYear} />
          {documentYears.includes(year) ? null : (
            <p role="note" className="rounded-md border border-border bg-muted/40 p-3 text-sm text-muted-foreground">
              No tax forms are imported for {year}, so the estimate uses only what you enter below.{" "}
              <Link to="/tax/import" className="underline underline-offset-2">
                Import tax forms
              </Link>{" "}
              to add the figures from your W-2s and 1099s.
            </p>
          )}
          {sel.selectedUserId === null ? (
            <PeopleOverview users={sel.users} year={year} onSelectUser={sel.setSelectedUserId} />
          ) : (
            <EstimateView
              key={`${sel.selectedUserId}-${year}`}
              userId={sel.selectedUserId}
              year={year}
              users={sel.users}
              onSelectUser={sel.setSelectedUserId}
            />
          )}
        </>
      )}
    </Page>
  );
}
