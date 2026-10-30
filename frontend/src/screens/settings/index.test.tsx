import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ToastContextProvider } from "@/components/ui/toast-provider";

const settings = {
  llm: {
    enabled: false,
    base_url: "",
    api_format: "openai_chat" as const,
    path: "",
    model: "",
    temperature: null,
    max_tokens: null,
    reasoning_effort: "",
    extra_body: {},
    extra_headers: {},
    structured_output: true,
    batch_size: 50,
    timeout_s: 120,
    confidence_threshold: 0.7,
    parse_pages_per_request: 0,
    mode: "none" as const,
    resolved_url: "",
    has_key: false,
  },
  privacy: { store_pdf_copies: true, store_extract_cache: false, pii_terms: [] },
  ingest: { always_confirm_extractor: false, default_extractor: "auto" as const, tax_llm_fallback: false, date_format_hints: [], default_currency: "USD" },
  server: { port: 8756 },
};

vi.mock("./lib/hooks", () => ({
  useSettingsQuery: () => ({ data: settings, isPending: false, isSuccess: true, isError: false, refetch: vi.fn() }),
  useUpdateSettings: () => ({ mutate: vi.fn(), isPending: false }),
  useUsersQuery: () => ({ data: [], isPending: false, isError: false, refetch: vi.fn() }),
  useAccountsQuery: () => ({ data: [] }),
  useIssuersQuery: () => ({ data: [] }),
  useCreateUser: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateUser: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteUser: () => ({ mutate: vi.fn(), isPending: false }),
  useCreateAccount: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateAccount: () => ({ mutate: vi.fn(), isPending: false }),
  useSetApiKey: () => ({ mutate: vi.fn(), isPending: false }),
  useClearApiKey: () => ({ mutate: vi.fn(), isPending: false }),
  useSecretStorageQuery: () => ({ data: undefined }),
  useSetFileStore: () => ({ mutate: vi.fn(), isPending: false }),
  useTaxParametersQuery: () => ({ data: undefined, isPending: true, isError: false, error: null }),
  useRefreshTaxParameters: () => ({ mutate: vi.fn(), isPending: false, isError: false, data: undefined }),
  useTestLlm: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useClassifyPreview: () => ({ isPending: false, isError: false, isSuccess: true, data: [] }),
}));

vi.mock("./lib/backup", () => ({
  MIN_PASSPHRASE_LENGTH: 8,
  backupFilename: () => "pfa-2026-01-01.pfabackup",
  saveBlob: vi.fn(),
  useExportBackup: () => ({ mutate: vi.fn(), isPending: false }),
  useRestoreBackup: () => ({ mutate: vi.fn(), isPending: false }),
}));

describe("SettingsScreen", () => {
  it("exports the screen contract (default component + screenMeta)", async () => {
    const mod = await import("./index");
    expect(mod.screenMeta).toEqual({ path: "/settings", title: "Settings" });
    expect(typeof mod.default).toBe("function");
  });

  function renderScreen(Screen: () => JSX.Element, entry = "/settings") {
    return render(
      <MemoryRouter initialEntries={[entry]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <ToastContextProvider>
          <Screen />
        </ToastContextProvider>
      </MemoryRouter>,
    );
  }

  it("renders every section", async () => {
    const { default: SettingsScreen } = await import("./index");
    renderScreen(SettingsScreen);
    expect(screen.getByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
    for (const title of [
      "Users and accounts",
      "LLM provider",
      "Privacy",
      "Tax parameters",
      "Preview what will be sent",
      "Backup & restore",
    ]) {
      expect(screen.getByRole("heading", { level: 2, name: title })).toBeInTheDocument();
    }
  });

  it("has an in-page nav whose links point at each section's anchor", async () => {
    const { default: SettingsScreen } = await import("./index");
    renderScreen(SettingsScreen);
    const nav = screen.getByRole("navigation", { name: "On this page" });
    const links = within(nav).getAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual([
      "Users and accounts",
      "LLM provider",
      "Privacy",
      "Tax parameters",
      "Preview what will be sent",
      "Backup & restore",
    ]);
    for (const link of links) {
      const id = link.getAttribute("href")!.slice(1);
      expect(document.getElementById(id)).toBeInTheDocument();
    }
  });

  it("scrolls to the section named by the URL hash", async () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    const { default: SettingsScreen } = await import("./index");
    renderScreen(SettingsScreen, "/settings#users");
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(scrollIntoView.mock.contexts[0]).toBe(document.getElementById("users"));
  });
});
