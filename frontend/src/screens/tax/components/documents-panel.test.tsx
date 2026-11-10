import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as React from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "@/api/client";
import { ToastContextProvider } from "@/components/ui/toast-provider";
import { Toaster } from "@/components/ui/toaster";

import { USERS, document, formDetail } from "../test-fixtures";
import { DocumentsPanel } from "./documents-panel";

vi.mock("@/api/client", async () => {
  const actual =
    await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return {
    ...actual,
    apiClient: { GET: vi.fn(), POST: vi.fn(), DELETE: vi.fn(), PATCH: vi.fn() },
  };
});

const get = apiClient.GET as unknown as ReturnType<typeof vi.fn>;
const post = apiClient.POST as unknown as ReturnType<typeof vi.fn>;
const patch = apiClient.PATCH as unknown as ReturnType<typeof vi.fn>;
const del = apiClient.DELETE as unknown as ReturnType<typeof vi.fn>;

let client: QueryClient;
let initialEntry = "/tax/documents";

function LocationProbe() {
  const { pathname, search } = useLocation();
  return <div data-testid="location">{pathname + search}</div>;
}

function Wrapper({ children }: { children: React.ReactNode }) {
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter
        initialEntries={[initialEntry]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <ToastContextProvider>
          {children}
          <Toaster />
        </ToastContextProvider>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

let detailForms = [formDetail()];
const ok = (data: unknown) => Promise.resolve({ data, error: undefined });

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  initialEntry = "/tax/documents";
  for (const m of [get, post, patch, del]) m.mockReset();
  detailForms = [formDetail()];
  get.mockImplementation((path: string) =>
    path === "/tax/documents/{document_id}"
      ? ok({ ...document(), forms: detailForms })
      : ok([]),
  );
  patch.mockImplementation(() => ok({}));
  post.mockImplementation(() => ok({}));
  del.mockImplementation(() => ok(undefined));
});

function renderPanel(docs = [document()], year: number | null = null) {
  return render(
    <DocumentsPanel
      documents={docs}
      users={USERS}
      selectedUserId={1}
      year={year}
    />,
    { wrapper: Wrapper },
  );
}

async function openFirst(name = "tax_w2_normal.pdf") {
  await userEvent.click(
    screen.getByRole("button", { name: `Review figures for ${name}` }),
  );
  return screen.findByTestId("document-detail");
}

describe("review figures disclosure", () => {
  it("expands lazily and shows forms, items and the provenance badge", async () => {
    renderPanel();
    const toggle = screen.getByRole("button", { name: /Review figures for/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(get).not.toHaveBeenCalledWith(
      "/tax/documents/{document_id}",
      expect.anything(),
    );
    const detail = await openFirst();
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(within(detail).getByText("Parsed")).toBeInTheDocument();
    expect(
      within(detail).getByRole("group", {
        name: "Income item: Wages and salary box 1",
      }),
    ).toBeInTheDocument();
    expect(within(detail).getByLabelText("Payer name")).toHaveValue(
      "Pinecrest Logistics LLC",
    );
  });

  it("edits an amount in dollars and sends integer cents", async () => {
    renderPanel();
    const detail = await openFirst();
    const row = within(detail).getByRole("group", {
      name: /Income item: Wages and salary/,
    });
    const amount = within(row).getByLabelText("Amount ($)");
    expect(amount).toHaveValue("85432.10");
    expect(
      within(row).queryByRole("button", { name: "Save item" }),
    ).not.toBeInTheDocument();
    await userEvent.clear(amount);
    await userEvent.type(amount, "$90,000.5{enter}");
    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith("/tax/income-items/{item_id}", {
        params: { path: { item_id: 100 }, query: { user_id: 1 } },
        body: expect.objectContaining({
          amount_minor: 9000050,
          category: "salary_wages",
        }) as unknown,
      }),
    );
  });

  it("refuses a bad amount without calling the server", async () => {
    renderPanel();
    const detail = await openFirst();
    const row = within(detail).getByRole("group", {
      name: /Income item: Wages and salary/,
    });
    const amount = within(row).getByLabelText("Amount ($)");
    await userEvent.clear(amount);
    await userEvent.type(amount, "12abc");
    await userEvent.click(
      within(row).getByRole("button", { name: "Save item" }),
    );
    expect(
      await within(row).findByText("Enter a valid dollar amount"),
    ).toBeInTheDocument();
    expect(amount).toHaveAttribute("aria-invalid", "true");
    expect(patch).not.toHaveBeenCalled();
  });

  it("rejects a negative tax amount", async () => {
    renderPanel();
    const detail = await openFirst();
    const row = within(detail).getByRole("group", {
      name: /Tax item: Federal income tax withheld/,
    });
    const amount = within(row).getByLabelText("Amount ($)");
    await userEvent.clear(amount);
    await userEvent.type(amount, "-5");
    expect(
      await within(row).findByText("Amount cannot be negative"),
    ).toBeInTheDocument();
  });

  it("adds an income item", async () => {
    renderPanel();
    const detail = await openFirst();
    await userEvent.click(
      within(detail).getByRole("button", { name: "Add income item" }),
    );
    const row = within(detail).getByRole("group", { name: "New income item" });
    await userEvent.selectOptions(
      within(row).getByLabelText("Category"),
      "interest",
    );
    await userEvent.type(within(row).getByLabelText("Box"), "1");
    await userEvent.type(within(row).getByLabelText("Amount ($)"), "25.5");
    await userEvent.click(
      within(row).getByRole("button", { name: "Add income item" }),
    );
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/tax/forms/{form_id}/income-items", {
        params: { path: { form_id: 10 }, query: { user_id: 1 } },
        body: {
          category: "interest",
          amount_minor: 2550,
          box: "1",
          label: null,
          is_memo: false,
        },
      }),
    );
  });

  it("deletes a tax item", async () => {
    renderPanel();
    const detail = await openFirst();
    const row = within(detail).getByRole("group", {
      name: /Tax item: Federal income tax withheld/,
    });
    await userEvent.click(
      within(row).getByRole("button", { name: "Delete item" }),
    );
    await waitFor(() =>
      expect(del).toHaveBeenCalledWith("/tax/tax-items/{item_id}", {
        params: { path: { item_id: 200 }, query: { user_id: 1 } },
      }),
    );
  });

  it("edits the payer name and validates last 4", async () => {
    renderPanel();
    const detail = await openFirst();
    const last4 = within(detail).getByLabelText("Payer ID last 4");
    await userEvent.clear(last4);
    await userEvent.type(last4, "12");
    await userEvent.click(
      within(detail).getByRole("button", { name: "Save form" }),
    );
    expect(
      await within(detail).findByText("Enter exactly 4 digits"),
    ).toBeInTheDocument();
    expect(patch).not.toHaveBeenCalled();
    await userEvent.clear(last4);
    await userEvent.type(last4, "9876");
    const payer = within(detail).getByLabelText("Payer name");
    await userEvent.clear(payer);
    await userEvent.type(payer, "New Corp");
    await userEvent.click(
      within(detail).getByRole("button", { name: "Save form" }),
    );
    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith("/tax/forms/{form_id}", {
        params: { path: { form_id: 10 }, query: { user_id: 1 } },
        body: { payer_name: "New Corp", payer_tin_last4: "9876" },
      }),
    );
  });

  it("confirms LLM values that need review", async () => {
    detailForms = [
      formDetail({ extraction_method: "llm", needs_review: true }),
    ];
    renderPanel();
    const detail = await openFirst();
    expect(
      within(detail).getByText("Read by LLM — needs review"),
    ).toBeInTheDocument();
    await userEvent.click(
      within(detail).getByRole("button", { name: "Confirm values" }),
    );
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/tax/forms/{form_id}/confirm", {
        params: { path: { form_id: 10 }, query: { user_id: 1 } },
      }),
    );
  });

  it("asks before deleting a form", async () => {
    renderPanel();
    const detail = await openFirst();
    await userEvent.click(
      within(detail).getByRole("button", { name: "Delete form" }),
    );
    expect(del).not.toHaveBeenCalled();
    await userEvent.click(
      within(detail).getByRole("button", { name: "Confirm delete form" }),
    );
    await waitFor(() =>
      expect(del).toHaveBeenCalledWith("/tax/forms/{form_id}", {
        params: { path: { form_id: 10 }, query: { user_id: 1 } },
      }),
    );
  });

  it("shows a toast with the server's 422 message", async () => {
    patch.mockImplementation(() =>
      Promise.resolve({
        data: undefined,
        error: { detail: [{ msg: "payer name looks like an SSN" }] },
        response: { status: 422 },
      }),
    );
    renderPanel();
    const detail = await openFirst();
    const payer = within(detail).getByLabelText("Payer name");
    await userEvent.clear(payer);
    await userEvent.type(payer, "123-45-6789");
    await userEvent.click(
      within(detail).getByRole("button", { name: "Save form" }),
    );
    expect(await screen.findByText("Could not save form")).toBeInTheDocument();
    expect(
      screen.getByText("payer name looks like an SSN"),
    ).toBeInTheDocument();
  });
});

describe("owner and moving a document", () => {
  it("shows the owner chip on every row, even for one user", () => {
    render(
      <DocumentsPanel
        documents={[document({ user_id: 2 })]}
        users={USERS}
        selectedUserId={null}
      />,
      {
        wrapper: Wrapper,
      },
    );
    expect(screen.getByTestId("tax-document-owner")).toHaveTextContent("Sam");
  });

  it("sends the document's own owner in All users mode", async () => {
    detailForms = [formDetail()];
    get.mockImplementation((path: string) =>
      path === "/tax/documents/{document_id}"
        ? ok({ ...document({ user_id: 2 }), forms: detailForms })
        : ok([]),
    );
    render(
      <DocumentsPanel
        documents={[document({ user_id: 2 })]}
        users={USERS}
        selectedUserId={null}
      />,
      {
        wrapper: Wrapper,
      },
    );
    const detail = await openFirst();
    expect(get).toHaveBeenCalledWith("/tax/documents/{document_id}", {
      params: { path: { document_id: 1 }, query: { user_id: 2 } },
    });
    await userEvent.click(
      within(detail).getByRole("button", { name: "Delete form" }),
    );
    await userEvent.click(
      within(detail).getByRole("button", { name: "Confirm delete form" }),
    );
    await waitFor(() =>
      expect(del).toHaveBeenCalledWith("/tax/forms/{form_id}", {
        params: { path: { form_id: 10 }, query: { user_id: 2 } },
      }),
    );
  });

  it("moves a document after a confirm step and shows a toast", async () => {
    renderPanel();
    await userEvent.selectOptions(
      screen.getByLabelText(/Move tax_w2_normal.pdf to another user/),
      "2",
    );
    expect(patch).not.toHaveBeenCalled();
    expect(
      screen.getByText("Move this document and its figures to Sam?"),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Confirm move" }));
    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith("/tax/documents/{document_id}", {
        params: { path: { document_id: 1 }, query: { user_id: 1 } },
        body: { user_id: 2 },
      }),
    );
    expect(await screen.findByText("Moved to Sam")).toBeInTheDocument();
  });

  it("collapses open editors after a move and uses the new owner afterwards", async () => {
    patch.mockImplementation(() =>
      ok({ ...document({ user_id: 2 }), forms: detailForms }),
    );
    renderPanel();
    await openFirst();
    await userEvent.selectOptions(
      screen.getByLabelText(/Move tax_w2_normal.pdf to another user/),
      "2",
    );
    await userEvent.click(screen.getByRole("button", { name: "Confirm move" }));
    await screen.findByText("Moved to Sam");
    expect(screen.queryByTestId("document-detail")).not.toBeInTheDocument();

    // The list prop still says user 1 (it refetches later); the row already uses the new owner.
    get.mockClear();
    await openFirst();
    expect(get).toHaveBeenCalledWith("/tax/documents/{document_id}", {
      params: { path: { document_id: 1 }, query: { user_id: 2 } },
    });
    expect(get).not.toHaveBeenCalledWith("/tax/documents/{document_id}", {
      params: { path: { document_id: 1 }, query: { user_id: 1 } },
    });
  });

  it("can cancel a move and hides the control with a single user", async () => {
    const { unmount } = renderPanel();
    await userEvent.selectOptions(
      screen.getByLabelText(/to another user/),
      "2",
    );
    await userEvent.click(screen.getByRole("button", { name: "Cancel move" }));
    expect(patch).not.toHaveBeenCalled();
    expect(screen.getByLabelText(/to another user/)).toBeInTheDocument();
    unmount();
    render(
      <DocumentsPanel
        documents={[document()]}
        users={[USERS[0]!]}
        selectedUserId={1}
      />,
      { wrapper: Wrapper },
    );
    expect(screen.queryByLabelText(/to another user/)).not.toBeInTheDocument();
  });

  it("shows the server's message when the move collides", async () => {
    patch.mockImplementation(() =>
      Promise.resolve({
        data: undefined,
        error: { detail: "Sam already has a W-2 form" },
        response: { status: 409 },
      }),
    );
    renderPanel();
    await userEvent.selectOptions(
      screen.getByLabelText(/to another user/),
      "2",
    );
    await userEvent.click(screen.getByRole("button", { name: "Confirm move" }));
    expect(
      await screen.findByText("Could not move document"),
    ).toBeInTheDocument();
  });
});

describe("manual entry", () => {
  const failed = document({
    id: 5,
    original_name: "scan.pdf",
    status: "unsupported",
    form_types: [],
    tax_year: 2024,
    error_detail: "Not a tax form",
  });

  it("adds a form to a failed document via Add figures manually", async () => {
    detailForms = [];
    renderPanel([failed]);
    await userEvent.click(
      screen.getByRole("button", { name: "Add figures manually" }),
    );
    const form = await screen.findByRole("form", { name: "Add form" });
    expect(
      screen.getByText("No figures were read. Add them manually."),
    ).toBeInTheDocument();
    await userEvent.selectOptions(
      within(form).getByLabelText("Form type"),
      "1099-int",
    );
    await userEvent.type(
      within(form).getByLabelText("Payer name"),
      "Harbor Bank",
    );
    await userEvent.click(
      within(form).getByRole("button", { name: "Add form" }),
    );
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/tax/documents/{document_id}/forms", {
        params: { path: { document_id: 5 }, query: { user_id: 1 } },
        body: expect.objectContaining({
          form_type: "1099-int",
          payer_name: "Harbor Bank",
          tax_year: 2024,
        }) as unknown,
      }),
    );
  });

  it("validates the add-form fields", async () => {
    detailForms = [];
    renderPanel([failed]);
    await userEvent.click(
      screen.getByRole("button", { name: "Add figures manually" }),
    );
    const form = await screen.findByRole("form", { name: "Add form" });
    await userEvent.type(within(form).getByLabelText("Payer ID last 4"), "123");
    await userEvent.click(
      within(form).getByRole("button", { name: "Add form" }),
    );
    expect(
      await within(form).findByText("Enter exactly 4 digits"),
    ).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("creates a manual document without a PDF and opens the editor", async () => {
    detailForms = [];
    post.mockImplementation(() =>
      ok(document({ id: 9, original_name: "Manual entry", form_types: [] })),
    );
    const docs = [
      document({ id: 9, original_name: "Manual entry", form_types: [] }),
    ];
    const { rerender } = renderPanel([]);
    await userEvent.click(
      screen.getByRole("button", { name: "Enter figures without a PDF" }),
    );
    expect(
      await screen.findByText("Enter the tax year first"),
    ).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Tax year"), "2024");
    await userEvent.click(
      screen.getByRole("button", { name: "Enter figures without a PDF" }),
    );
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/tax/documents/manual", {
        body: { user_id: 1, tax_year: 2024 },
      }),
    );
    // The new document is addressed through the URL; its row opens the editor once it is listed.
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        "doc=9&add=1",
      ),
    );
    rerender(
      <DocumentsPanel documents={docs} users={USERS} selectedUserId={1} />,
    );
    expect(
      await screen.findByRole("form", { name: "Add form" }),
    ).toBeInTheDocument();
    // `add` is consumed so a reload does not reopen the editor; the document stays addressed.
    await waitFor(() =>
      expect(screen.getByTestId("location")).not.toHaveTextContent("add=1"),
    );
    expect(screen.getByTestId("location")).toHaveTextContent("doc=9");
  });

  it("prefills the manual-entry year from the year filter", () => {
    renderPanel([], 2023);
    expect(screen.getByLabelText("Tax year")).toHaveValue(2023);
  });

  it("asks which user to add for when All users is selected", () => {
    render(
      <DocumentsPanel documents={[]} users={USERS} selectedUserId={null} />,
      { wrapper: Wrapper },
    );
    expect(
      screen.getByRole("combobox", { name: "Add for user" }),
    ).toBeInTheDocument();
  });
});

describe("deep link and year filter", () => {
  it("opens the add-form editor for ?doc=<id>&add=1", async () => {
    initialEntry = "/tax/documents?doc=5&add=1";
    detailForms = [];
    const failed = document({
      id: 5,
      original_name: "scan.pdf",
      status: "unsupported",
      form_types: [],
      tax_year: 2024,
    });
    renderPanel([document(), failed]);
    const form = await screen.findByRole("form", { name: "Add form" });
    expect(form).toBeInTheDocument();
    // Only the addressed document opens.
    expect(screen.getAllByRole("form", { name: "Add form" })).toHaveLength(1);
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        "/tax/documents?doc=5",
      ),
    );
    expect(screen.getByTestId("location")).not.toHaveTextContent("add=1");
  });

  it("lists only the filtered year, but never hides the addressed document", () => {
    initialEntry = "/tax/documents?doc=3";
    const docs = [
      document({ id: 1, original_name: "a-2024.pdf", tax_year: 2024 }),
      document({ id: 2, original_name: "b-2023.pdf", tax_year: 2023 }),
      document({ id: 3, original_name: "c-2022.pdf", tax_year: 2022 }),
    ];
    render(
      <DocumentsPanel
        documents={docs}
        users={USERS}
        selectedUserId={1}
        year={2024}
      />,
      { wrapper: Wrapper },
    );
    expect(screen.getByText("a-2024.pdf")).toBeInTheDocument();
    expect(screen.queryByText("b-2023.pdf")).not.toBeInTheDocument();
    expect(screen.getByText("c-2022.pdf")).toBeInTheDocument();
  });

  it("says so when the filtered year has no documents", () => {
    renderPanel([document()], 2020);
    expect(screen.getByText("No tax documents for 2020.")).toBeInTheDocument();
  });
});

describe("read pages with the LLM", () => {
  const PREVIEW = {
    mode: "remote",
    resolved_url: "https://api.example.com/v1",
    model: "m1",
    pages: [2],
    requests: ["=== PAGE 2 ===\nInterest 1,234.56"],
    total_chars: 33,
    redactions: 2,
  };
  const PAGES = [
    {
      page_number: 1,
      char_count: 100,
      suggested: false,
      excluded_reason: null,
    },
    { page_number: 2, char_count: 200, suggested: true, excluded_reason: null },
    {
      page_number: 3,
      char_count: 300,
      suggested: false,
      excluded_reason: "Form 1040 pages are never sent.",
    },
  ];

  function setupLlm(mode: "none" | "remote" = "remote") {
    get.mockImplementation(
      (path: string, opts?: { params?: { query?: { pages?: string } } }) => {
        if (path === "/settings")
          return ok({ llm: { mode, base_url: "https://api.example.com/v1" } });
        if (path === "/tax/documents/{document_id}/pages") return ok(PAGES);
        if (path === "/tax/documents/{document_id}/llm-preview") {
          return ok({
            ...PREVIEW,
            pages: (opts?.params?.query?.pages ?? "2").split(",").map(Number),
          });
        }
        if (path === "/tax/documents/{document_id}")
          return ok({ ...document(), forms: detailForms });
        return ok([]);
      },
    );
  }
  const failed = () =>
    document({
      id: 5,
      status: "unsupported",
      form_types: [],
      has_pdf: true,
      page_count: 3,
    });

  async function openSection() {
    await userEvent.click(
      await screen.findByRole("button", { name: /Read pages of .* with LLM/ }),
    );
    return screen.findByTestId("llm-read-section");
  }

  it("is offered only when an LLM is configured", async () => {
    setupLlm("none");
    renderPanel([failed()]);
    await screen.findByText("tax_w2_normal.pdf");
    await waitFor(() => expect(get).toHaveBeenCalledWith("/settings"));
    expect(
      screen.queryByRole("button", { name: /with LLM/ }),
    ).not.toBeInTheDocument();
  });

  it("preselects the fallback pages, previews collapsed, and sends after confirming", async () => {
    setupLlm();
    post.mockImplementation(() =>
      ok({
        document: { ...document({ id: 5 }), forms: [] },
        pages: [2],
        requests_sent: 1,
        inserted_forms: 1,
        skipped_duplicate_forms: 0,
        merged_forms: 0,
        income_item_count: 2,
        tax_item_count: 0,
        warnings: [],
      }),
    );
    renderPanel([failed()]);
    const section = await openSection();
    await within(section).findByLabelText("Page 2");
    expect(within(section).getByLabelText("Page 2")).toBeChecked();
    expect(within(section).getByLabelText("Page 1")).not.toBeChecked();
    expect(within(section).getByLabelText("Page 3")).toBeDisabled();
    expect(
      within(section).getByAltText("Page 1 preview").getAttribute("src"),
    ).toContain("/api/tax/documents/5/pages/1/image");

    // The preview is not fetched until expanded.
    expect(get).not.toHaveBeenCalledWith(
      "/tax/documents/{document_id}/llm-preview",
      expect.anything(),
    );
    await userEvent.click(
      within(section).getByRole("button", { name: "Show what would be sent" }),
    );
    expect(await within(section).findByTestId("llm-request")).toHaveTextContent(
      "Interest 1,234.56",
    );

    // Changing the selection re-queries with exactly the chosen pages.
    await userEvent.click(within(section).getByLabelText("Page 1"));
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith(
        "/tax/documents/{document_id}/llm-preview",
        {
          params: {
            path: { document_id: 5 },
            query: { user_id: 1, pages: "1,2" },
          },
        },
      ),
    );

    expect(post).not.toHaveBeenCalled();
    await userEvent.click(
      within(section).getByRole("button", {
        name: "Send selected pages to LLM",
      }),
    );
    expect(
      within(section).getByText(
        /Send the redacted text of 2 pages to api\.example\.com\?/,
      ),
    ).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
    await userEvent.click(
      within(section).getByRole("button", { name: "Confirm send" }),
    );
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/tax/documents/{document_id}/llm-fallback",
        {
          params: { path: { document_id: 5 }, query: { user_id: 1 } },
          body: { pages: [1, 2] },
        },
      ),
    );
    expect(
      await screen.findByText("Read 1 form with the LLM"),
    ).toBeInTheDocument();
  });

  it("disables Send while the expanded preview of the new selection loads, not when collapsed", async () => {
    setupLlm();
    renderPanel([failed()]);
    const section = await openSection();
    await within(section).findByLabelText("Page 2");
    const send = () =>
      within(section).getByRole("button", { name: "Send selected pages to LLM" });
    // Collapsed: sending is allowed even though no preview was loaded.
    expect(send()).toBeEnabled();
    await userEvent.click(
      within(section).getByRole("button", { name: "Show what would be sent" }),
    );
    await within(section).findByTestId("llm-request");
    expect(send()).toBeEnabled();

    // Now the preview of the next selection hangs: Send is blocked and old text is gone.
    let release: (v: unknown) => void = () => {};
    const base = get.getMockImplementation() as (p: string, o?: unknown) => unknown;
    get.mockImplementation((path: string, opts?: unknown) =>
      path === "/tax/documents/{document_id}/llm-preview"
        ? new Promise((r) => (release = r))
        : base(path, opts),
    );
    await userEvent.click(within(section).getByLabelText("Page 1"));
    await within(section).findByText("Loading preview…");
    expect(send()).toBeDisabled();
    expect(within(section).queryByTestId("llm-request")).not.toBeInTheDocument();
    release({ data: { ...PREVIEW, pages: [1, 2] }, error: undefined });
    await within(section).findByTestId("llm-request");
    expect(send()).toBeEnabled();
  });

  const PDF = () =>
    new File(["%PDF-1.4"], "tax_w2_normal.pdf", { type: "application/pdf" });
  const noCopy = () => ({ ...failed(), has_pdf: false, staged_until: null });

  it("asks to select the original file again when no PDF copy is kept", async () => {
    setupLlm();
    renderPanel([noCopy()]);
    const section = await openSection();
    expect(
      within(section).getByText(
        /select the original file again \(it isn.t kept after import for privacy\)/,
      ),
    ).toBeInTheDocument();
    expect(
      within(section).getByLabelText("Select tax_w2_normal.pdf again"),
    ).toBeInTheDocument();
    expect(within(section).queryByLabelText("Page 1")).not.toBeInTheDocument();
    expect(get).not.toHaveBeenCalledWith(
      "/tax/documents/{document_id}/pages",
      expect.anything(),
    );
  });

  it("says PDF not found when a kept copy was moved out or deleted", async () => {
    setupLlm();
    renderPanel([{ ...noCopy(), pdf_missing: true }]);
    const section = await openSection();
    expect(within(section).getByTestId("pdf-not-found")).toHaveTextContent(
      /PDF not found.*no longer in your documents folder/s,
    );
    // It still offers the way out (select the same file again), without claiming it was never kept.
    expect(
      within(section).getByLabelText("Select tax_w2_normal.pdf again"),
    ).toBeInTheDocument();
    expect(within(section).queryByText(/isn.t kept after import/)).not.toBeInTheDocument();
    expect(get).not.toHaveBeenCalledWith(
      "/tax/documents/{document_id}/pages",
      expect.anything(),
    );
  });

  it("does not say PDF not found for a document that never kept a copy", async () => {
    setupLlm();
    renderPanel([noCopy()]);
    const section = await openSection();
    expect(within(section).queryByTestId("pdf-not-found")).not.toBeInTheDocument();
  });

  it("shows the page picker after the same file is provided", async () => {
    setupLlm();
    post.mockImplementation((path: string) =>
      path === "/tax/documents/{document_id}/source"
        ? ok({ pages: PAGES, expires_at: "2026-10-26T13:00:00+00:00" })
        : ok({}),
    );
    const view = renderPanel([noCopy()]);
    const section = await openSection();
    await userEvent.upload(
      within(section).getByLabelText("Select tax_w2_normal.pdf again"),
      PDF(),
    );
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/tax/documents/{document_id}/source", {
        params: { path: { document_id: 5 }, query: { user_id: 1 } },
        body: expect.any(FormData) as unknown,
      }),
    );
    // The server now reports the staged copy on the document.
    view.rerender(
      <DocumentsPanel
        documents={[{ ...noCopy(), staged_until: "2026-10-26T13:00:00+00:00" }]}
        users={USERS}
        selectedUserId={1}
      />,
    );
    expect(await screen.findByLabelText("Page 2")).toBeChecked();
  });

  it("shows the mismatch error and keeps asking for the file", async () => {
    setupLlm();
    post.mockImplementation(() =>
      Promise.resolve({
        data: undefined,
        error: { detail: "This isn't the same file as tax_w2_normal.pdf" },
      }),
    );
    renderPanel([noCopy()]);
    const section = await openSection();
    await userEvent.upload(
      within(section).getByLabelText("Select tax_w2_normal.pdf again"),
      PDF(),
    );
    expect(
      await screen.findByText("Could not use that file"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("This isn't the same file as tax_w2_normal.pdf"),
    ).toBeInTheDocument();
    expect(within(section).queryByLabelText("Page 1")).not.toBeInTheDocument();
  });

  it("asks again when the temporary copy has expired", async () => {
    setupLlm();
    get.mockImplementation((path: string) => {
      if (path === "/settings")
        return ok({
          llm: { mode: "remote", base_url: "https://api.example.com/v1" },
        });
      if (path === "/tax/documents/{document_id}/pages") {
        return Promise.resolve({
          data: undefined,
          error: {
            detail: {
              code: "pdf_needed",
              message: "The original file isn't available any more",
            },
          },
        });
      }
      return ok([]);
    });
    renderPanel([{ ...noCopy(), staged_until: "2026-10-26T13:00:00+00:00" }]);
    const section = await openSection();
    expect(
      await within(section).findByText(/The temporary copy has expired/),
    ).toBeInTheDocument();
    expect(
      within(section).getByLabelText("Select tax_w2_normal.pdf again"),
    ).toBeInTheDocument();
  });

  it("gives each pane's detail region a unique id in compare mode", () => {
    render(
      <>
        <DocumentsPanel
          documents={[document()]}
          users={USERS}
          selectedUserId={1}
        />
        <DocumentsPanel
          documents={[document()]}
          users={USERS}
          selectedUserId={1}
        />
      </>,
      { wrapper: Wrapper },
    );
    const toggles = screen.getAllByRole("button", {
      name: /Review figures for/,
    });
    const ids = toggles.map((t) => t.getAttribute("aria-controls"));
    expect(new Set(ids).size).toBe(2);
  });
});
