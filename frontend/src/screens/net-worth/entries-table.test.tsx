import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "@/api/client";

import { EntriesTable } from "./entries-table";
import { USERS, Wrapper, entry } from "./test-utils";

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, apiClient: { GET: vi.fn(), POST: vi.fn(), PATCH: vi.fn(), DELETE: vi.fn() } };
});

const ENTRIES = [
  entry({ id: 1, month: 3, account_name: "Chase checking", balance_minor: 100000, note: "after rent" }),
  entry({ id: 2, month: 2, account_name: "Ally", category: "savings", balance_minor: 250000 }),
];

beforeEach(() => {
  vi.clearAllMocks();
  (apiClient.PATCH as ReturnType<typeof vi.fn>).mockResolvedValue({ data: {}, error: undefined });
  (apiClient.DELETE as ReturnType<typeof vi.fn>).mockResolvedValue({ data: undefined, error: undefined });
});

describe("EntriesTable", () => {
  it("renders rows newest first and an empty state", () => {
    const { unmount } = render(<EntriesTable entries={ENTRIES} users={USERS} showUser={false} />, { wrapper: Wrapper });
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Mar 2025");
    expect(rows[0]).toHaveTextContent("$1,000.00");
    expect(rows[0]).toHaveTextContent("after rent");
    expect(rows[1]).toHaveTextContent("Feb 2025");
    unmount();
    render(<EntriesTable entries={[]} users={USERS} showUser={false} />, { wrapper: Wrapper });
    expect(screen.getByText("No entries yet")).toBeInTheDocument();
  });

  it("filters by period and shows the owner column in all-users mode", async () => {
    render(<EntriesTable entries={[ENTRIES[0]!, { ...ENTRIES[1]!, user_id: 2 }]} users={USERS} showUser />, { wrapper: Wrapper });
    expect(screen.getByRole("columnheader", { name: "User" })).toBeInTheDocument();
    expect(screen.getByText("Sam")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Filter by period"), "2025-02");
    expect(screen.queryByText("Chase checking")).not.toBeInTheDocument();
    expect(screen.getByText("Ally")).toBeInTheDocument();
  });

  it("edits balance and note in place via PATCH", async () => {
    render(<EntriesTable entries={ENTRIES} users={USERS} showUser={false} />, { wrapper: Wrapper });
    await userEvent.click(screen.getByRole("button", { name: "Edit Chase checking Mar 2025" }));
    const input = screen.getByLabelText("Balance for Chase checking Mar 2025");
    await userEvent.clear(input);
    await userEvent.type(input, "1,500.25");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(apiClient.PATCH).toHaveBeenCalledWith("/net-worth/entries/{id}", {
        params: { path: { id: 1 } },
        body: { balance_minor: 150025, note: "after rent" },
      }),
    );
  });

  it("rejects an invalid edit without calling the API", async () => {
    render(<EntriesTable entries={ENTRIES} users={USERS} showUser={false} />, { wrapper: Wrapper });
    await userEvent.click(screen.getByRole("button", { name: "Edit Chase checking Mar 2025" }));
    const input = screen.getByLabelText("Balance for Chase checking Mar 2025");
    await userEvent.clear(input);
    await userEvent.type(input, "-3");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("non-negative");
    expect(apiClient.PATCH).not.toHaveBeenCalled();
  });

  it("deletes after a second confirming click", async () => {
    render(<EntriesTable entries={ENTRIES} users={USERS} showUser={false} />, { wrapper: Wrapper });
    await userEvent.click(screen.getByRole("button", { name: "Delete Ally Feb 2025" }));
    expect(apiClient.DELETE).not.toHaveBeenCalled();
    await userEvent.click(within(screen.getByTestId("entries-table")).getByRole("button", { name: "Confirm delete Ally Feb 2025" }));
    await waitFor(() =>
      expect(apiClient.DELETE).toHaveBeenCalledWith("/net-worth/entries/{id}", { params: { path: { id: 2 } } }),
    );
  });
});
