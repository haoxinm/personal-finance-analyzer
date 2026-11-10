import * as React from "react";

import { Page, PageHeader } from "@/components/primitives/page";
import { ErrorState, LoadingState } from "@/components/primitives/states";
import { Card, CardContent } from "@/components/ui/card";
import { useSelectedUser } from "@/hooks/use-selected-user";

import { DocumentsPanel } from "./components/documents-panel";
import { useTaxDocuments, useTaxSummary, useTaxYearFilter } from "./hooks";
import { supersededByDocument } from "./logic";

export const screenMeta = { path: "/tax/documents", title: "Documents" };

/**
 * Income & tax documents: every imported tax form PDF (and hand-entered document) with its status,
 * figures to review or correct, owner, and manual entry. New files are uploaded on the Import tab.
 * Filterable by year via `?year=`.
 */
export default function TaxDocumentsScreen() {
  const sel = useSelectedUser();
  const documentsQuery = useTaxDocuments(sel.selectedUserId, !sel.isLoading);
  const [year, setYear] = useTaxYearFilter();
  // Only for the "replaced by corrected form" badges: a failed summary just shows no badges.
  const summaryQuery = useTaxSummary(sel.userIds, !sel.isLoading);
  const superseded = React.useMemo(
    () => supersededByDocument(summaryQuery.data ?? []),
    [summaryQuery.data],
  );
  const documents = React.useMemo(() => documentsQuery.data ?? [], [documentsQuery.data]);
  const years = React.useMemo(
    () =>
      [
        ...new Set(
          documents.flatMap((d) => (d.tax_year === null ? [] : [d.tax_year])),
        ),
      ].sort((a, b) => b - a),
    [documents],
  );
  // The URL may name a year no document has (or one that was just deleted): keep it selectable.
  const yearOptions = year !== null && !years.includes(year) ? [year, ...years] : years;

  return (
    <Page>
      <PageHeader
        title="Tax documents"
        description="The tax forms behind your income and tax figures. Review what was read, correct it, or enter figures by hand."
        actions={
          yearOptions.length > 0 ? (
            <label className="flex items-center gap-2 text-sm text-muted-foreground">
              Year
              <select
                aria-label="Filter by year"
                className="h-9 rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm"
                value={year ?? ""}
                onChange={(e) =>
                  setYear(e.target.value === "" ? null : Number(e.target.value))
                }
              >
                <option value="">All years</option>
                {yearOptions.map((y) => (
                  <option key={y} value={y}>
                    {y}
                  </option>
                ))}
              </select>
            </label>
          ) : null
        }
      />

      <Card>
        <CardContent className="pt-4">
          {documentsQuery.error ? (
            <ErrorState
              description={documentsQuery.error.message}
              onRetry={() => void documentsQuery.refetch()}
            />
          ) : sel.isLoading || documentsQuery.isLoading ? (
            <LoadingState rows={3} />
          ) : (
            <DocumentsPanel
              documents={documents}
              users={sel.users}
              selectedUserId={sel.selectedUserId}
              year={year}
              superseded={superseded}
            />
          )}
        </CardContent>
      </Card>
    </Page>
  );
}
