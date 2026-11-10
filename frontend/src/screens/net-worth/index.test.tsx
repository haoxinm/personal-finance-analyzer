import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as React from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "@/api/client";
import { ToastContextProvider } from "@/components/ui/toast-provider";
import { WithUserSwitcher } from "@/test/with-user-switcher";

import NetWorthScreen, { screenMeta } from "./index";

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, apiClient: { GET: vi.fn() } };
});

beforeEach(() => {
  window.localStorage.clear();
});

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        initialEntries={["/net-worth"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <ToastContextProvider>
          <WithUserSwitcher>{children}</WithUserSwitcher>
        </ToastContextProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

const EMPTY_NET_WORTH = {
  currency: "USD",
  user_id: null,
  periods: [],
  series: [],
  account_series: [],
  snapshot: null,
};

function mockGet(responses: Record<string, unknown>) {
  (apiClient.GET as ReturnType<typeof vi.fn>).mockImplementation((path: string) => {
    const fallback = path === "/net-worth/summary" ? EMPTY_NET_WORTH : [];
    return Promise.resolve({ data: responses[path] ?? fallback, error: undefined });
  });
}

const summaryCalls = () =>
  (apiClient.GET as ReturnType<typeof vi.fn>).mock.calls.filter(([path]) => path === "/net-worth/summary");

describe("NetWorthScreen", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("exposes its route metadata", () => {
    expect(screenMeta).toEqual({ path: "/net-worth", title: "Net worth" });
  });

  it("renders its own heading and the net-worth section, and follows the user chip", async () => {
    mockGet({
      "/users": [
        { id: 1, name: "Alex", is_default: true },
        { id: 2, name: "Sam", is_default: false },
      ],
      "/accounts": [],
    });

    render(<NetWorthScreen />, { wrapper });

    expect(screen.getByRole("heading", { level: 1, name: "Net worth overview" })).toBeInTheDocument();
    expect(await screen.findByTestId("net-worth-section")).toBeInTheDocument();
    expect(await screen.findByText("No balances yet")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("radio", { name: "Sam" }));

    await waitFor(() => {
      const last = summaryCalls().at(-1)![1] as { params: { query: { user_id?: number } } };
      expect(last.params.query.user_id).toBe(2);
    });
  });

  it("offers a currency select when accounts span currencies, and re-queries with it", async () => {
    mockGet({
      "/users": [{ id: 1, name: "Alex", is_default: true }],
      "/accounts": [
        { id: 1, user_id: 1, issuer_id: null, account_type: "credit", currency: "USD" },
        { id: 2, user_id: 1, issuer_id: null, account_type: "checking", currency: "EUR" },
      ],
    });

    render(<NetWorthScreen />, { wrapper });

    await userEvent.selectOptions(await screen.findByRole("combobox", { name: "Currency" }), "EUR");

    await waitFor(() => {
      const last = summaryCalls().at(-1)![1] as { params: { query: { currency?: string } } };
      expect(last.params.query.currency).toBe("EUR");
    });
  });

  it("hides the currency select with a single currency", async () => {
    mockGet({ "/users": [{ id: 1, name: "Alex", is_default: true }], "/accounts": [] });
    render(<NetWorthScreen />, { wrapper });
    await screen.findByTestId("net-worth-section");
    expect(screen.queryByRole("combobox", { name: "Currency" })).not.toBeInTheDocument();
  });
});
