import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

import type { components } from "@/api/client";
import { ToastContextProvider } from "@/components/ui/toast-provider";
import { Toaster } from "@/components/ui/toaster";

export type Entry = components["schemas"]["BalanceEntry"];
export type Summary = components["schemas"]["NetWorthSummary"];
export type Snapshot = components["schemas"]["NetWorthSnapshot"];

export function Wrapper({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }));
  return (
    <QueryClientProvider client={client}>
      <ToastContextProvider>
        {children}
        <Toaster />
      </ToastContextProvider>
    </QueryClientProvider>
  );
}

export const USERS = [
  { id: 1, name: "Alex", is_default: true },
  { id: 2, name: "Sam", is_default: false },
];

export function entry(over: Partial<Entry> = {}): Entry {
  return {
    id: 1,
    user_id: 1,
    category: "checking",
    account_name: "Chase checking",
    year: 2025,
    month: 3,
    balance_minor: 100000,
    currency: "USD",
    note: null,
    created_at: "2025-03-01T00:00:00Z",
    updated_at: "2025-03-01T00:00:00Z",
    ...over,
  };
}

export const SNAPSHOT: Snapshot = {
  period: "2025-03",
  assets_minor: 850000,
  liabilities_minor: 30000,
  net_worth_minor: 820000,
  previous_period: "2025-02",
  change_minor: 20000,
  categories: [
    { category: "checking", balance_minor: 100000, signed_minor: 100000 },
    { category: "savings", balance_minor: 250000, signed_minor: 250000 },
    { category: "investment", balance_minor: 500000, signed_minor: 500000 },
    { category: "credit_card", balance_minor: 30000, signed_minor: -30000 },
  ],
  accounts: [
    { user_id: 1, category: "checking", account_name: "Chase checking", balance_minor: 100000, signed_minor: 100000, as_of: "2025-03", carried: false },
    { user_id: 1, category: "savings", account_name: "Ally", balance_minor: 250000, signed_minor: 250000, as_of: "2025-02", carried: true },
    { user_id: 1, category: "investment", account_name: "Fidelity", balance_minor: 500000, signed_minor: 500000, as_of: "2025-03", carried: false },
    { user_id: 1, category: "credit_card", account_name: "Visa", balance_minor: 30000, signed_minor: -30000, as_of: "2025-03", carried: false },
  ],
};

export const SUMMARY: Summary = {
  currency: "USD",
  user_id: 1,
  periods: ["2025-02", "2025-03"],
  series: [
    { period: "2025-02", by_category: { checking: 90000, savings: 250000, investment: 480000, credit_card: 40000 }, assets_minor: 820000, liabilities_minor: 40000, net_worth_minor: 780000 },
    { period: "2025-03", by_category: { checking: 100000, savings: 250000, investment: 500000, credit_card: 30000 }, assets_minor: 850000, liabilities_minor: 30000, net_worth_minor: 820000 },
  ],
  account_series: [
    { user_id: 1, category: "checking", account_name: "Chase checking", points: [
      { period: "2025-02", balance_minor: 90000, carried: false },
      { period: "2025-03", balance_minor: 100000, carried: false },
    ] },
    { user_id: 1, category: "savings", account_name: "Ally", points: [
      { period: "2025-02", balance_minor: 250000, carried: false },
      { period: "2025-03", balance_minor: 250000, carried: true },
    ] },
  ],
  snapshot: SNAPSHOT,
};

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
export function stubResizeObserver(vi: { stubGlobal: (n: string, v: unknown) => unknown }) {
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
}
