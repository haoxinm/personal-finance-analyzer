import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";
import { MemoryRouter, useLocation } from "react-router-dom";

import { ToastContextProvider } from "@/components/ui/toast-provider";
import { WithUserSwitcher } from "@/test/with-user-switcher";

import { PAYERS, USERS, document, summary } from "./test-fixtures";

/** Mirrors the shell's providers around one tax screen at `initialEntry`. */
export function makeWrapper(initialEntry: string) {
  return function Wrapper({ children }: { children: React.ReactNode }) {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter
          initialEntries={[initialEntry]}
          future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        >
          <ToastContextProvider>
            <WithUserSwitcher>{children}</WithUserSwitcher>
          </ToastContextProvider>
          <LocationProbe />
        </MemoryRouter>
      </QueryClientProvider>
    );
  };
}

export function LocationProbe() {
  const { pathname, search } = useLocation();
  return <div data-testid="location">{pathname + search}</div>;
}

/** `apiClient.GET` responses (by path) for a user with two years of data and two documents. */
export const FULL_RESPONSES = {
  "/users": USERS,
  "/tax/years": [2024, 2023],
  "/tax/summary": [
    summary(),
    summary({ tax_year: 2023, total_income_minor: 9000000 }),
  ],
  "/tax/income-by-payer": PAYERS,
  "/tax/documents": [
    document(),
    document({
      id: 2,
      original_name: "scan.pdf",
      status: "unsupported",
      form_types: [],
      tax_year: null,
      error_detail: "Not a recognised tax form.",
    }),
  ],
};

export class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
