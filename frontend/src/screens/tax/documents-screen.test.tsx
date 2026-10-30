import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "@/api/client";

import TaxDocumentsScreen, { screenMeta } from "./documents-screen";
import { USERS, document, summary } from "./test-fixtures";
import { FULL_RESPONSES as FULL, makeWrapper } from "./test-utils";

vi.mock("@/api/client", async () => {
  const actual =
    await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return {
    ...actual,
    apiClient: { GET: vi.fn(), POST: vi.fn(), DELETE: vi.fn() },
  };
});

const get = apiClient.GET as unknown as ReturnType<typeof vi.fn>;
const del = apiClient.DELETE as unknown as ReturnType<typeof vi.fn>;

function mockGet(responses: Record<string, unknown>) {
  get.mockImplementation((path: string) =>
    Promise.resolve({ data: responses[path] ?? [], error: undefined }),
  );
}

function renderScreen(entry = "/tax/documents") {
  return render(<TaxDocumentsScreen />, { wrapper: makeWrapper(entry) });
}

beforeEach(() => {
  window.localStorage.clear();
  get.mockReset();
  del.mockReset();
});
afterEach(() => vi.restoreAllMocks());

describe("TaxDocumentsScreen", () => {
  it("exposes its route metadata", () => {
    expect(screenMeta).toEqual({ path: "/tax/documents", title: "Documents" });
  });

  it("labels the manual-entry tax year by its visible text only", async () => {
    mockGet({ "/users": [USERS[0]] });
    renderScreen();
    const input = await screen.findByLabelText("Tax year");
    expect(input).not.toHaveAttribute("aria-label");
    expect(screen.getByText("No tax documents imported yet.")).toBeInTheDocument();
  });

  it("requires a confirm before deleting a document", async () => {
    mockGet(FULL);
    del.mockResolvedValue({ error: undefined });
    renderScreen();
    await userEvent.click(
      await screen.findByRole("button", { name: "Delete tax_w2_normal.pdf" }),
    );
    expect(del).not.toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: "Confirm delete" }),
    );
    await waitFor(() =>
      expect(del).toHaveBeenCalledWith("/tax/documents/{document_id}", {
        params: { path: { document_id: 1 }, query: { user_id: 1 } },
      }),
    );
  });

  it("lists badges and errors for documents and asks for a user when All is selected", async () => {
    mockGet(FULL);
    renderScreen();
    const docs = await screen.findAllByTestId("tax-document");
    expect(docs).toHaveLength(2);
    expect(within(docs[0]!).getByText("W-2")).toBeInTheDocument();
    expect(within(docs[1]!).getByText("Unsupported")).toBeInTheDocument();
    expect(
      within(docs[1]!).getByText("Not a recognised tax form."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "Add for user" }),
    ).toBeInTheDocument();
  });

  it("badges a document whose form a corrected form replaced", async () => {
    mockGet({
      ...FULL,
      "/tax/documents": [
        document({ id: 1, original_name: "w2-original.pdf" }),
        document({ id: 3, original_name: "w2-corrected.pdf" }),
      ],
      "/tax/summary": [
        summary({
          superseded_forms: [
            {
              form_id: 5,
              document_id: 1,
              user_id: 1,
              form_type: "w2",
              superseded_by_form_id: 6,
            },
          ],
        }),
      ],
    });
    renderScreen();
    const docs = await screen.findAllByTestId("tax-document");
    const badge = await within(docs[0]!).findByTestId("tax-document-superseded");
    expect(badge).toHaveTextContent("Replaced by corrected form");
    expect(badge).toHaveAttribute("title", expect.stringMatching(/W-2 .* not counted/));
    expect(within(docs[1]!).queryByTestId("tax-document-superseded")).not.toBeInTheDocument();
  });

  it("does not offer uploading here (that is the Import tab)", async () => {
    mockGet(FULL);
    renderScreen();
    await screen.findAllByTestId("tax-document");
    expect(screen.queryByLabelText("Choose tax form PDFs")).not.toBeInTheDocument();
  });

  it("filters by ?year= and keeps the filter in the URL", async () => {
    mockGet({
      ...FULL,
      "/tax/documents": [
        document({ id: 1, original_name: "a-2024.pdf", tax_year: 2024 }),
        document({ id: 2, original_name: "b-2023.pdf", tax_year: 2023 }),
      ],
    });
    renderScreen("/tax/documents?year=2023");
    expect(await screen.findByText("b-2023.pdf")).toBeInTheDocument();
    expect(screen.queryByText("a-2024.pdf")).not.toBeInTheDocument();
    const select = screen.getByRole("combobox", { name: "Filter by year" });
    expect(select).toHaveValue("2023");

    await userEvent.selectOptions(select, "");
    expect(await screen.findByText("a-2024.pdf")).toBeInTheDocument();
    expect(screen.getByTestId("location")).not.toHaveTextContent("year=");

    await userEvent.selectOptions(select, "2024");
    await waitFor(() =>
      expect(screen.queryByText("b-2023.pdf")).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId("location")).toHaveTextContent("year=2024");
  });
});
