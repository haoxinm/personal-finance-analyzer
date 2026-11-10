import * as React from "react";

import { useSelectedUser } from "@/hooks/use-selected-user";
import { useSpendQueryParams } from "@/hooks/use-spend-query-params";
import { useAccounts } from "@/screens/dashboard/hooks";
import { cn } from "@/utils";

import { NetWorthSection } from "./net-worth-section";

/**
 * The Net worth screen: snapshot, trends and balance entry, per user (or everyone combined).
 * Independent of spending data, so it renders even before any statement is imported. The
 * display currency round-trips through `?currency=` like the Spending filters do.
 */
export default function NetWorthScreen() {
  const sel = useSelectedUser();
  const [params, setParams] = useSpendQueryParams();
  const accountsQuery = useAccounts();
  const currency = params.currency ?? "USD";

  const currencyOptions = React.useMemo(() => {
    const set = new Set<string>([currency]);
    for (const account of accountsQuery.data ?? []) set.add(account.currency);
    return Array.from(set).sort();
  }, [accountsQuery.data, currency]);

  return (
    <div className="flex flex-col gap-6 py-2">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Net worth</h1>
          <p className="text-sm text-muted-foreground">
            Balances across accounts, how they split by type, and how they change over time.
            {sel.users.length > 1 && sel.selectedUser ? ` Showing ${sel.selectedUser.name}.` : ""}
          </p>
        </div>
        {currencyOptions.length > 1 ? (
          <label className="flex flex-col gap-1 text-xs text-muted-foreground">
            Currency
            <select
              aria-label="Currency"
              className={cn(
                "flex h-9 w-24 items-center rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm",
                "transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              )}
              value={currency}
              onChange={(e) => setParams({ currency: e.target.value })}
            >
              {currencyOptions.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
        ) : null}
      </div>

      {sel.isLoading ? null : <NetWorthSection userId={sel.selectedUserId} users={sel.users} currency={currency} />}
    </div>
  );
}

export const screenMeta = { path: "/net-worth", title: "Net worth" };
