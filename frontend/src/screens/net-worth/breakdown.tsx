import * as React from "react";

import type { components } from "@/api/client";
import { Money } from "@/components/primitives/money";
import { cn } from "@/utils";

import { NET_WORTH_CATEGORY_COLORS } from "./chart-colors";
import { ASSET_CATEGORIES, categoryLabel, changeDirection, periodLabel } from "./logic";

type Snapshot = components["schemas"]["NetWorthSnapshot"];
type User = components["schemas"]["User"];

export interface BreakdownProps {
  snapshot: Snapshot;
  users: readonly User[];
  currency: string;
}

/** Headline tiles: net worth with its change vs the previous entered period, plus assets
 * and credit card owed. The change is stated in words and with a glyph, never colour alone. */
export function NetWorthHeadline({ snapshot, currency }: { snapshot: Snapshot; currency: string }) {
  const dir = changeDirection(snapshot.change_minor);
  const change = snapshot.change_minor ?? 0;
  return (
    <div className="grid gap-3 sm:grid-cols-3" data-testid="net-worth-headline">
      <div className="rounded-lg border border-border p-4 sm:col-span-1">
        <p className="text-xs font-medium text-muted-foreground">
          Net worth · {periodLabel(snapshot.period)}
        </p>
        <p className="mt-1 text-3xl font-semibold" data-testid="net-worth-total">
          <Money tone="neutral" minorUnits={snapshot.net_worth_minor} currency={currency} />
        </p>
        <p
          className={cn(
            "mt-1 text-sm",
            dir === "up" && "text-inflow",
            dir === "down" && "text-outflow",
            (dir === "flat" || dir === "none") && "text-muted-foreground",
          )}
          data-testid="net-worth-change"
        >
          {dir === "none" || !snapshot.previous_period ? (
            "No earlier period to compare"
          ) : dir === "flat" ? (
            <>No change vs {periodLabel(snapshot.previous_period)}</>
          ) : (
            <>
              <span aria-hidden="true">{dir === "up" ? "▲" : "▼"} </span>
              {dir === "up" ? "Up " : "Down "}
              <Money
                tone="neutral"
                minorUnits={Math.abs(change)}
                currency={currency}
                className="text-current"
              />{" "}
              vs {periodLabel(snapshot.previous_period)}
            </>
          )}
        </p>
      </div>
      <div className="rounded-lg border border-border p-4">
        <p className="text-xs font-medium text-muted-foreground">Assets</p>
        <p className="mt-1 text-xl font-semibold">
          <Money tone="neutral" minorUnits={snapshot.assets_minor} currency={currency} />
        </p>
        <p className="text-xs text-muted-foreground">Checking + savings + investment</p>
      </div>
      <div className="rounded-lg border border-border p-4">
        <p className="text-xs font-medium text-muted-foreground">Credit card owed</p>
        <p className="mt-1 text-xl font-semibold">
          <Money tone="neutral" minorUnits={snapshot.liabilities_minor} currency={currency} />
        </p>
        <p className="text-xs text-muted-foreground">Subtracted from net worth</p>
      </div>
    </div>
  );
}

/** Per-category and per-account table. Credit card shows as a negative (it reduces net worth);
 * carried accounts say which month their balance comes from. */
export function Breakdown({ snapshot, users, currency }: BreakdownProps) {
  const owners = new Set(snapshot.accounts.map((a) => a.user_id));
  const nameOf = (id: number) => users.find((u) => u.id === id)?.name ?? `User ${id}`;

  return (
    <table className="w-full text-sm" data-testid="net-worth-breakdown">
      <caption className="sr-only">Net worth by category and account</caption>
      <thead>
        <tr className="border-b border-border text-left text-xs text-muted-foreground">
          <th scope="col" className="py-2 pr-3 font-medium">Category / account</th>
          <th scope="col" className="py-2 pr-3 text-right font-medium">Balance</th>
          <th scope="col" className="py-2 text-right font-medium">Share of assets</th>
        </tr>
      </thead>
      <tbody>
        {snapshot.categories.map((cat) => {
          const isAsset = (ASSET_CATEGORIES as readonly string[]).includes(cat.category);
          const accounts = snapshot.accounts.filter((a) => a.category === cat.category);
          const share = (minor: number) =>
            isAsset && snapshot.assets_minor > 0
              ? `${((minor / snapshot.assets_minor) * 100).toFixed(0)}%`
              : "–";
          return (
            <React.Fragment key={cat.category}>
              <tr className="border-b border-border/60 font-medium" data-testid={`category-${cat.category}`}>
                <th scope="row" className="py-2 pr-3 text-left font-medium">
                  <span
                    aria-hidden="true"
                    className="mr-2 inline-block h-2.5 w-2.5 rounded-sm"
                    style={{ background: NET_WORTH_CATEGORY_COLORS[cat.category] }}
                  />
                  {categoryLabel(cat.category)}
                  {!isAsset ? <span className="ml-1 font-normal text-muted-foreground">(owed)</span> : null}
                </th>
                <td className="py-2 pr-3 text-right">
                  <Money tone="neutral" minorUnits={cat.signed_minor} currency={currency} />
                </td>
                <td className="py-2 text-right tabular-nums text-muted-foreground">
                  {share(cat.balance_minor)}
                </td>
              </tr>
              {accounts.map((a) => (
                <tr
                  key={`${a.user_id}:${a.category}:${a.account_name}`}
                  className="border-b border-border/40 text-muted-foreground"
                >
                  <th scope="row" className="py-1.5 pr-3 pl-6 text-left font-normal">
                    {a.account_name}
                    {owners.size > 1 ? ` (${nameOf(a.user_id)})` : ""}
                    {a.carried ? (
                      <span className="ml-2 text-xs italic">carried from {periodLabel(a.as_of)}</span>
                    ) : null}
                  </th>
                  <td className="py-1.5 pr-3 text-right">
                    <Money tone="neutral" minorUnits={a.signed_minor} currency={currency} />
                  </td>
                  <td className="py-1.5 text-right tabular-nums">{share(a.balance_minor)}</td>
                </tr>
              ))}
            </React.Fragment>
          );
        })}
      </tbody>
      <tfoot>
        <tr className="border-t border-border">
          <th scope="row" className="py-2 pr-3 text-left font-medium">Assets</th>
          <td className="py-2 pr-3 text-right">
            <Money tone="neutral" minorUnits={snapshot.assets_minor} currency={currency} />
          </td>
          <td />
        </tr>
        <tr>
          <th scope="row" className="py-1 pr-3 text-left font-medium">Liabilities</th>
          <td className="py-1 pr-3 text-right">
            <Money tone="neutral" minorUnits={-snapshot.liabilities_minor} currency={currency} />
          </td>
          <td />
        </tr>
        <tr className="border-t border-border font-semibold">
          <th scope="row" className="py-2 pr-3 text-left font-semibold">Net worth</th>
          <td className="py-2 pr-3 text-right" data-testid="breakdown-net-worth">
            <Money tone="neutral" minorUnits={snapshot.net_worth_minor} currency={currency} />
          </td>
          <td />
        </tr>
      </tfoot>
    </table>
  );
}
