import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ALEX = { id: 1, name: "Alex", is_default: true };
const SAM = { id: 2, name: "Sam", is_default: false };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

async function renderImport(path: string, legacyStickyId?: number) {
  vi.resetModules();
  if (legacyStickyId !== undefined) {
    window.localStorage.setItem("pfa:import:sticky-user-id", String(legacyStickyId));
  }
  const uploads: string[] = [];
  // Record the multipart `user_id` field as the upload code appends it.
  // eslint-disable-next-line @typescript-eslint/unbound-method
  const original = FormData.prototype.append;
  vi.spyOn(FormData.prototype, "append").mockImplementation(function (this: FormData, ...args: Parameters<FormData["append"]>) {
    if (args[0] === "user_id") uploads.push(typeof args[1] === "string" ? args[1] : "");
    return original.apply(this, args);
  });
  const fetchMock = vi.fn<typeof fetch>((input) => {
    const request = input instanceof Request ? input : new Request(input);
    const url = new URL(request.url);
    if (url.pathname === "/api/users") return Promise.resolve(json([ALEX, SAM]));
    if (url.pathname === "/api/settings") return Promise.resolve(json({ llm: { mode: "none" } }));
    if (url.pathname === "/api/statements" && request.method === "POST") {
      return Promise.resolve(json({ detail: "stop here" }, 422));
    }
    return Promise.resolve(json([]));
  });
  vi.stubGlobal("fetch", fetchMock);
  const { default: ImportScreen } = await import("./index");
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <ImportScreen />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { uploads };
}

const pdf = () => new File(["%PDF-1.4"], "a.pdf", { type: "application/pdf" });

describe("Import upload target", () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("uses the globally selected user and shows no picker", async () => {
    const { uploads } = await renderImport("/spending/import?user=2");
    expect(await screen.findByTestId("import-target")).toHaveTextContent("Importing for Sam");
    expect(screen.queryByLabelText("Importing for")).not.toBeInTheDocument();
    await userEvent.upload(screen.getByLabelText("Choose statement PDFs"), pdf());
    await waitFor(() => expect(uploads).toEqual(["2"]));
  });

  it("asks which user to import for when All users is selected, and blocks upload until chosen", async () => {
    const { uploads } = await renderImport("/spending/import?user=all");
    const picker = await screen.findByLabelText("Importing for");
    expect(picker).toHaveValue("");
    expect(screen.queryByTestId("import-target")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Choose statement PDFs")).toBeDisabled();

    await userEvent.selectOptions(picker, "Sam");
    expect(screen.getByLabelText("Choose statement PDFs")).toBeEnabled();
    await userEvent.upload(screen.getByLabelText("Choose statement PDFs"), pdf());
    await waitFor(() => expect(uploads).toEqual(["2"]));
  });

  it("ignores a stale legacy sticky Import user and starts at 'Choose a user…'", async () => {
    await renderImport("/spending/import?user=all", 1);
    expect(await screen.findByLabelText("Importing for")).toHaveValue("");
    expect(screen.getByLabelText("Choose statement PDFs")).toBeDisabled();
  });
});
