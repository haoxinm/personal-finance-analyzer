import { Link } from "react-router-dom";

import { Page, PageHeader } from "@/components/primitives/page";
import { EmptyState, ErrorState, LoadingState } from "@/components/primitives/states";
import { Button } from "@/components/ui/button";
import { useSelectedUser } from "@/hooks/use-selected-user";

import { YearPicker } from "./components/year-picker";
import { EstimateView } from "./estimate/estimate-view";
import { PeopleOverview } from "./estimate/people-overview";
import { useTaxYear, useTaxYears } from "./hooks";

export const screenMeta = { path: "/tax/estimate", title: "Estimate" };

const EMPTY_TEXT = "Import a W-2, 1099 or 1040 to estimate your tax";

/**
 * Income & Tax estimate: a simple federal income tax estimate for one person and year, computed from
 * the imported source forms and a few manual inputs (filing status, deduction, credits, payments).
 * The Form 1040, when there is one, is only a cross-check. With "All users" selected the screen shows
 * one summary card per person, because the estimate needs a person.
 */
export default function TaxEstimateScreen() {
  const sel = useSelectedUser();
  const yearsQuery = useTaxYears(sel.userIds, !sel.isLoading);
  const years = yearsQuery.data ?? [];
  const [year, setYear] = useTaxYear(years);

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
      ) : years.length === 0 || year === null ? (
        <EmptyState
          title={EMPTY_TEXT}
          description="The estimate starts from your imported tax forms."
          action={
            <Button asChild size="sm" variant="outline">
              <Link to="/tax/import">Import tax forms</Link>
            </Button>
          }
        />
      ) : (
        <>
          <YearPicker years={years} value={year} onChange={setYear} />
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
