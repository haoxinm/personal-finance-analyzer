import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "@/api/client";

import TaxImportScreen, { screenMeta } from "./import-screen";
import { USERS, document } from "./test-fixtures";
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
const post = apiClient.POST as unknown as ReturnType<typeof vi.fn>;

function mockGet(responses: Record<string, unknown>) {
  get.mockImplementation((path: string) =>
    Promise.resolve({ data: responses[path] ?? [], error: undefined }),
  );
}

const ok = (data: unknown) => Promise.resolve({ data, error: undefined });

function importResponse(over: Record<string, unknown> = {}) {
  return {
    document: document(),
    status: "duplicate",
    inserted_forms: 0,
    skipped_duplicate_forms: 1,
    income_item_count: 0,
    tax_item_count: 0,
    warnings: [],
    error: null,
    ...over,
  };
}

function renderScreen(entry = "/tax/import") {
  return render(<TaxImportScreen />, { wrapper: makeWrapper(entry) });
}

/** The drop zone is disabled until the users load (it needs one to upload for). */
async function dropZoneReady() {
  const zone = await screen.findByRole("button", { name: /Drag tax forms here/ });
  await waitFor(() => expect(zone).toHaveAttribute("aria-disabled", "false"));
}

const pdf = (name: string) =>
  new File(["%PDF-1.4"], name, { type: "application/pdf" });

beforeEach(() => {
  window.localStorage.clear();
  get.mockReset();
  post.mockReset();
});
afterEach(() => vi.restoreAllMocks());

describe("TaxImportScreen", () => {
  it("exposes its route metadata", () => {
    expect(screenMeta).toEqual({ path: "/tax/import", title: "Import" });
  });

  it("shows the upload zone, a clearly named year override and a privacy note", async () => {
    mockGet({ "/users": [USERS[0]] });
    renderScreen();
    expect(await screen.findByLabelText("Choose tax form PDFs")).toBeInTheDocument();
    const input = screen.getByLabelText(/Tax year override \(only if a form/);
    expect(input).not.toHaveAttribute("aria-label");
    expect(screen.getByTestId("tax-privacy-note")).toHaveTextContent(
      /last 4 digits/,
    );
  });

  it("asks for a user when All is selected, uploads for them and reports the outcome", async () => {
    mockGet(FULL);
    post.mockResolvedValue({ data: importResponse(), error: undefined });
    renderScreen();
    await userEvent.selectOptions(
      await screen.findByRole("combobox", { name: "Upload for user" }),
      "2",
    );
    await userEvent.upload(screen.getByLabelText("Choose tax form PDFs"), pdf("w2.pdf"));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    const [path, init] = post.mock.calls[0] as [string, { body: FormData }];
    expect(path).toBe("/tax/documents");
    expect(init.body.get("user_id")).toBe("2");
    expect(init.body.has("tax_year")).toBe(false);
    expect(await screen.findByTestId("upload-outcomes")).toHaveTextContent(
      /Already imported/,
    );
  });

  it("sends the tax year override", async () => {
    mockGet({ "/users": [USERS[0]] });
    post.mockResolvedValue({ data: importResponse(), error: undefined });
    renderScreen();
    await dropZoneReady();
    await userEvent.type(screen.getByLabelText(/Tax year override/), "2022");
    await userEvent.upload(screen.getByLabelText("Choose tax form PDFs"), pdf("w2.pdf"));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    const [, init] = post.mock.calls[0] as [string, { body: FormData }];
    expect(init.body.get("tax_year")).toBe("2022");
  });

  it("lists skipped-from files and warnings", async () => {
    mockGet({ "/users": [USERS[0]] });
    post.mockImplementation(() =>
      ok(
        importResponse({
          document: document({ id: 7 }),
          status: "parsed",
          inserted_forms: 1,
          skipped_from: [
            { document_id: 1, original_name: "x.pdf", form_type: "w2" },
          ],
          income_item_count: 1,
          warnings: ["Payer name was inferred from the page header."],
        }),
      ),
    );
    renderScreen();
    await dropZoneReady();
    await userEvent.upload(screen.getByLabelText("Choose tax form PDFs"), pdf("new.pdf"));
    const list = await screen.findByTestId("upload-outcomes");
    expect(
      within(list).getByText("Imported 1 form.", { exact: false }),
    ).toBeInTheDocument();
    expect(
      within(list).getByText(
        '1 form was already imported from "x.pdf" and was skipped.',
      ),
    ).toBeInTheDocument();
    expect(
      within(list).getByText("Payer name was inferred from the page header."),
    ).toBeInTheDocument();
  });

  it("links a failed file's manual entry to the Documents tab", async () => {
    mockGet({ "/users": [USERS[0]] });
    post.mockImplementation(() =>
      ok(
        importResponse({
          document: document({ id: 12, status: "unsupported" }),
          status: "unsupported",
          skipped_duplicate_forms: 0,
          error: "Not a tax form",
        }),
      ),
    );
    renderScreen();
    await dropZoneReady();
    await userEvent.upload(screen.getByLabelText("Choose tax form PDFs"), pdf("scan.pdf"));
    const link = await screen.findByRole("link", { name: "Add figures manually" });
    expect(link).toHaveAttribute("href", "/tax/documents?doc=12&add=1");
  });
});
