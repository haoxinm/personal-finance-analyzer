import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "@/api/client";

import { BalanceEntryForm } from "./entry-form";
import { USERS, Wrapper, entry } from "./test-utils";

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, apiClient: { GET: vi.fn(), POST: vi.fn(), PATCH: vi.fn(), DELETE: vi.fn() } };
});

function post(status: number) {
  (apiClient.POST as ReturnType<typeof vi.fn>).mockResolvedValue({
    data: entry(),
    error: undefined,
    response: { status },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  post(201);
});

describe("BalanceEntryForm", () => {
  it("validates before submitting", async () => {
    render(<BalanceEntryForm users={USERS} selectedUserId={1} entries={[]} />, { wrapper: Wrapper });
    await userEvent.click(screen.getByRole("button", { name: "Save balance" }));
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.map((a) => a.textContent).join(" ")).toMatch(/account name/i);
    expect(alerts.map((a) => a.textContent).join(" ")).toMatch(/non-negative amount/i);
    expect(apiClient.POST).not.toHaveBeenCalled();
  });

  it("posts the entry in minor units and clears only the amount", async () => {
    render(<BalanceEntryForm users={USERS} selectedUserId={2} entries={[]} />, { wrapper: Wrapper });
    await userEvent.selectOptions(screen.getByLabelText("Category"), "savings");
    await userEvent.type(screen.getByLabelText("Account name"), "Ally");
    await userEvent.selectOptions(screen.getByLabelText("Month"), "3");
    await userEvent.clear(screen.getByLabelText("Year"));
    await userEvent.type(screen.getByLabelText("Year"), "2025");
    await userEvent.type(screen.getByLabelText(/Balance/), "1,234.56");
    await userEvent.type(screen.getByLabelText("Note (optional)"), "bonus");
    await userEvent.click(screen.getByRole("button", { name: "Save balance" }));

    await waitFor(() =>
      expect(apiClient.POST).toHaveBeenCalledWith("/net-worth/entries", {
        body: {
          user_id: 2,
          category: "savings",
          account_name: "Ally",
          year: 2025,
          month: 3,
          balance_minor: 123456,
          currency: "USD",
          note: "bonus",
        },
      }),
    );
    expect(await screen.findByText("Saved")).toBeInTheDocument();
    expect(screen.getByLabelText(/Balance/)).toHaveValue("");
    expect(screen.getByLabelText("Account name")).toHaveValue("Ally");
  });

  it("says when an existing entry was updated (HTTP 200)", async () => {
    post(200);
    render(<BalanceEntryForm users={USERS} selectedUserId={1} entries={[]} />, { wrapper: Wrapper });
    await userEvent.type(screen.getByLabelText("Account name"), "Chase");
    await userEvent.type(screen.getByLabelText(/Balance/), "5");
    await userEvent.click(screen.getByRole("button", { name: "Save balance" }));
    expect(await screen.findByText("Updated existing entry")).toBeInTheDocument();
  });

  it("explains credit card amounts as owed", async () => {
    render(<BalanceEntryForm users={USERS} selectedUserId={1} entries={[]} />, { wrapper: Wrapper });
    expect(screen.queryByText(/what you owe/i)).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Category"), "credit_card");
    expect(screen.getByText(/what you owe/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Amount owed/)).toBeInTheDocument();
  });

  it("offers account suggestions from existing entries for the category", async () => {
    const entries = [
      entry({ id: 1, account_name: "Chase checking" }),
      entry({ id: 2, account_name: "Ally", category: "savings" }),
    ];
    render(<BalanceEntryForm users={USERS} selectedUserId={1} entries={entries} />, { wrapper: Wrapper });
    const list = screen.getByTestId("account-suggestions");
    expect(Array.from(list.querySelectorAll("option")).map((o) => o.value)).toEqual(["Chase checking"]);
    await userEvent.selectOptions(screen.getByLabelText("Category"), "savings");
    expect(Array.from(list.querySelectorAll("option")).map((o) => o.value)).toEqual(["Ally"]);
  });

  it("asks who the balance is for only in All-users mode (default user preselected)", async () => {
    const { unmount } = render(<BalanceEntryForm users={USERS} selectedUserId={1} entries={[]} />, { wrapper: Wrapper });
    expect(screen.queryByLabelText("Account owner")).not.toBeInTheDocument();
    unmount();

    render(<BalanceEntryForm users={USERS} selectedUserId={null} entries={[]} />, { wrapper: Wrapper });
    const owner = screen.getByLabelText("Account owner");
    expect(owner).toHaveValue("1");
    expect(screen.queryByRole("option", { name: "All users" })).not.toBeInTheDocument();
    await userEvent.selectOptions(owner, "2");
    await userEvent.type(screen.getByLabelText("Account name"), "Wells");
    await userEvent.type(screen.getByLabelText(/Balance/), "10");
    await userEvent.click(screen.getByRole("button", { name: "Save balance" }));
    await waitFor(() =>
      expect(apiClient.POST).toHaveBeenCalledWith(
        "/net-worth/entries",
        expect.objectContaining({ body: expect.objectContaining({ user_id: 2 }) as unknown }),
      ),
    );
  });

  it("shows a destructive toast on server error", async () => {
    (apiClient.POST as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: undefined,
      error: { detail: "user not found" },
      response: { status: 404 },
    });
    render(<BalanceEntryForm users={USERS} selectedUserId={1} entries={[]} />, { wrapper: Wrapper });
    await userEvent.type(screen.getByLabelText("Account name"), "X");
    await userEvent.type(screen.getByLabelText(/Balance/), "1");
    await userEvent.click(screen.getByRole("button", { name: "Save balance" }));
    expect(await screen.findByText("Could not save balance")).toBeInTheDocument();
  });
});
