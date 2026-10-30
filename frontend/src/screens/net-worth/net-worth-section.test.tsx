import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "@/api/client";

import { NetWorthSection } from "./net-worth-section";
import { SUMMARY, USERS, Wrapper, entry, stubResizeObserver } from "./test-utils";

stubResizeObserver(vi);

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, apiClient: { GET: vi.fn(), POST: vi.fn(), PATCH: vi.fn(), DELETE: vi.fn() } };
});

const get = apiClient.GET as ReturnType<typeof vi.fn>;

function mock(summary: unknown, entries: unknown = [entry()]) {
  get.mockImplementation((path: string) =>
    Promise.resolve({ data: path === "/net-worth/summary" ? summary : entries, error: undefined }),
  );
}

beforeEach(() => vi.clearAllMocks());

describe("NetWorthSection", () => {
  it("shows a loading state, then the snapshot, charts and tables", async () => {
    mock(SUMMARY);
    render(<NetWorthSection userId={1} users={USERS} currency="USD" />, { wrapper: Wrapper });
    expect(screen.getAllByRole("status", { name: "Loading" }).length).toBeGreaterThan(0);
    expect(await screen.findByTestId("net-worth-total")).toHaveTextContent("$8,200.00");
    expect(screen.getByTestId("net-worth-pie")).toBeInTheDocument();
    expect(screen.getByTestId("net-worth-trend-category")).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Add balance" })).toBeInTheDocument();
    expect(within(await screen.findByTestId("entries-table")).getByText("Chase checking")).toBeInTheDocument();
    expect(screen.getByText("Net worth · Alex")).toBeInTheDocument();
  });

  it("re-requests the summary with the chosen period", async () => {
    mock(SUMMARY);
    render(<NetWorthSection userId={1} users={USERS} currency="USD" />, { wrapper: Wrapper });
    await screen.findByTestId("net-worth-total");
    const select = screen.getByLabelText("Period");
    expect(select).toHaveValue("2025-03");
    await userEvent.selectOptions(select, "2025-02");
    await waitFor(() => {
      const calls = get.mock.calls.filter(([p]) => p === "/net-worth/summary");
      const last = calls.at(-1)![1] as { params: { query: { period?: string; user_id?: number } } };
      expect(last.params.query.period).toBe("2025-02");
      expect(last.params.query.user_id).toBe(1);
    });
  });

  it("omits user_id for all users", async () => {
    mock({ ...SUMMARY, user_id: null });
    render(<NetWorthSection userId={null} users={USERS} currency="USD" />, { wrapper: Wrapper });
    await screen.findByTestId("net-worth-total");
    const call = get.mock.calls.find(([p]) => p === "/net-worth/summary")!;
    expect((call[1] as { params: { query: { user_id?: number } } }).params.query.user_id).toBeUndefined();
    expect(screen.getByText("Net worth · all users")).toBeInTheDocument();
  });

  it("shows an empty state but still the form when there are no balances", async () => {
    mock({ currency: "USD", user_id: 1, periods: [], series: [], account_series: [], snapshot: null }, []);
    render(<NetWorthSection userId={1} users={USERS} currency="USD" />, { wrapper: Wrapper });
    expect(await screen.findByText("No balances yet")).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Add balance" })).toBeInTheDocument();
    expect(screen.queryByTestId("net-worth-trend-category")).not.toBeInTheDocument();
  });

  it("tolerates an unexpected empty-array payload", async () => {
    mock([], []);
    render(<NetWorthSection userId={1} users={USERS} currency="USD" />, { wrapper: Wrapper });
    expect(await screen.findByText("No balances yet")).toBeInTheDocument();
  });

  it("shows an error state with retry", async () => {
    get.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/net-worth/summary"
          ? { data: undefined, error: { detail: "boom" } }
          : { data: [], error: undefined },
      ),
    );
    render(<NetWorthSection userId={1} users={USERS} currency="USD" />, { wrapper: Wrapper });
    expect(await screen.findByText("Could not load net worth")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
