import { render, screen } from "@testing-library/react";
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
  useSettingsQuery: () => ({ data: settings, isPending: false, isError: false, refetch: vi.fn() }),
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
  useTestLlm: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  useClassifyPreview: () => ({ isPending: false, isError: false, isSuccess: true, data: [] }),
}));

vi.mock("./lib/backup", () => ({
  MIN_PASSPHRASE_LENGTH: 8,
  backupFilename: () => "spend-analyzer-2026-01-01.spendbackup",
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

  it("renders every section", async () => {
    const { default: SettingsScreen } = await import("./index");
    render(
      <ToastContextProvider>
        <SettingsScreen />
      </ToastContextProvider>,
    );
    expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
    expect(screen.getByText("Users and accounts")).toBeInTheDocument();
    expect(screen.getByText("LLM provider")).toBeInTheDocument();
    expect(screen.getByText("Privacy")).toBeInTheDocument();
    expect(screen.getByText("Preview what will be sent")).toBeInTheDocument();
    expect(screen.getByText("Backup & restore")).toBeInTheDocument();
  });
});
