import * as React from "react";

import type { components } from "@/api/client";
import { EmptyState, ErrorState, LoadingState } from "@/components/primitives/states";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/utils";

import { useBalanceEntries, useNetWorthSummary } from "./api";
import { Breakdown, NetWorthHeadline } from "./breakdown";
import { EntriesTable } from "./entries-table";
import { BalanceEntryForm } from "./entry-form";
import { periodLabel } from "./logic";
import { NetWorthPie } from "./net-worth-pie";
import { NetWorthTrends } from "./net-worth-trends";

type User = components["schemas"]["User"];

export interface NetWorthSectionProps {
  /** Selected user id, or null for all users combined. */
  userId: number | null;
  users: readonly User[];
  currency: string;
}

/** The Net worth screen's body: snapshot (headline, breakdown, pie), trends, and the entry
 * form + table. Independent of spending data, so it renders even before any import. */
export function NetWorthSection({ userId, users, currency }: NetWorthSectionProps) {
  // Unique per instance: compare mode renders two copies of this screen.
  const uid = React.useId();
  const [period, setPeriod] = React.useState<string | undefined>(undefined);
  const summaryQuery = useNetWorthSummary({ userId, currency, period });
  const entriesQuery = useBalanceEntries(userId);
  const summary = summaryQuery.data;
  const entries = entriesQuery.data;

  // A remembered period that does not exist for this user/currency falls back to "latest".
  React.useEffect(() => {
    if (period && summary && !summary.periods.includes(period)) setPeriod(undefined);
  }, [period, summary]);

  const snapshot = summary?.snapshot ?? null;
  const periods = React.useMemo(() => [...(summary?.periods ?? [])].reverse(), [summary?.periods]);
  const userLabel = userId === null ? "all users" : (users.find((u) => u.id === userId)?.name ?? "");

  let body: React.ReactNode;
  if (summaryQuery.error) {
    const err = summaryQuery.error;
    body = (
      <ErrorState
        title="Could not load net worth"
        description={err instanceof Error ? err.message : String(err)}
        onRetry={() => void summaryQuery.refetch()}
      />
    );
  } else if (summaryQuery.isLoading) {
    body = <LoadingState rows={3} />;
  } else if (!snapshot) {
    body = (
      <EmptyState
        title="No balances yet"
        description={`Add your first balance below to see net worth for ${userLabel || "this user"}.`}
      />
    );
  } else {
    body = (
      <div
        className={cn("flex flex-col gap-4", summaryQuery.isPlaceholderData && "opacity-60")}
        aria-busy={summaryQuery.isPlaceholderData}
      >
        <NetWorthHeadline snapshot={snapshot} currency={currency} />
        <div className="grid gap-6 lg:grid-cols-2">
          <div className="overflow-x-auto">
            <Breakdown snapshot={snapshot} users={users} currency={currency} />
          </div>
          <div>
            <h4 className="mb-2 text-xs font-medium text-muted-foreground">Where your assets are</h4>
            <NetWorthPie snapshot={snapshot} currency={currency} />
          </div>
        </div>
      </div>
    );
  }

  return (
    <section aria-labelledby={`${uid}-net-worth-title`} className="flex flex-col gap-4" data-testid="net-worth-section">
      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-3">
          <CardTitle id={`${uid}-net-worth-title`} className="text-base">
            Net worth{userLabel ? ` · ${userLabel}` : ""}
          </CardTitle>
          {periods.length > 0 ? (
            <div className="flex items-center gap-2">
              <label htmlFor={`${uid}-nw-period`} className="text-xs font-medium text-muted-foreground">
                Period
              </label>
              <select
                id={`${uid}-nw-period`}
                aria-label="Period"
                className="h-8 rounded-md border border-input bg-transparent px-2 text-sm"
                value={period ?? snapshot?.period ?? ""}
                onChange={(e) => setPeriod(e.target.value || undefined)}
              >
                {periods.map((p) => (
                  <option key={p} value={p}>
                    {periodLabel(p)}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
        </CardHeader>
        <CardContent>{body}</CardContent>
      </Card>

      {summary && summary.series.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>Over time</CardTitle>
          </CardHeader>
          <CardContent>
            <NetWorthTrends summary={summary} users={users} currency={currency} />
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Add a balance</CardTitle>
        </CardHeader>
        <CardContent>
          <BalanceEntryForm
            users={users}
            selectedUserId={userId}
            entries={entries ?? []}
          />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Entries</CardTitle>
        </CardHeader>
        <CardContent>
          {entriesQuery.error ? (
            <ErrorState
              title="Could not load entries"
              description={entriesQuery.error instanceof Error ? entriesQuery.error.message : String(entriesQuery.error)}
              onRetry={() => void entriesQuery.refetch()}
            />
          ) : entriesQuery.isLoading ? (
            <LoadingState rows={2} />
          ) : (
            <EntriesTable entries={entries ?? []} users={users} showUser={userId === null && users.length > 1} />
          )}
        </CardContent>
      </Card>
    </section>
  );
}
