import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "@/api/client";

import type { Settings, TaxParameters } from "../lib/hooks";
import { TaxParametersSection } from "./tax-parameters-section";

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, apiClient: { GET: vi.fn(), POST: vi.fn() } };
});

const get = apiClient.GET as unknown as ReturnType<typeof vi.fn>;
const post = apiClient.POST as unknown as ReturnType<typeof vi.fn>;

const SETTINGS: Settings = {
  llm: {
    enabled: false,
    base_url: "",
    api_format: "openai_chat",
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
    mode: "none",
    resolved_url: "",
    has_key: false,
  },
  privacy: { store_pdf_copies: true, store_extract_cache: false, pii_terms: [] },
  ingest: {
    always_confirm_extractor: false,
    default_extractor: "auto",
    tax_llm_fallback: false,
    date_format_hints: [],
    default_currency: "USD",
  },
  server: { port: 8756 },
};

function parameters(over: Partial<TaxParameters> = {}): TaxParameters {
  const year = (tax_year: number) => ({
    tax_year,
    sources: [],
    standard_deduction_minor: {},
    additional_standard_deduction_unmarried_minor: 0,
    additional_standard_deduction_married_minor: 0,
    capital_loss_limit_minor: 0,
    capital_loss_limit_mfs_minor: 0,
    ss_wage_base_minor: 0,
    elective_deferral_limit_minor: 0,
    niit_threshold_minor: {},
    additional_medicare_threshold_minor: {},
  });
  return {
    source: "bundled",
    params_version: 3,
    bundled_version: 3,
    cached_version: null,
    override_version: null,
    last_checked: null,
    last_check_outcome: null,
    auto_update: false,
    years: [2022, 2023, 2024, 2025, 2026].map(year),
    ...over,
  };
}

function renderSection(settings: Settings = SETTINGS, onSave = vi.fn(), saving = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  render(<TaxParametersSection settings={settings} onSave={onSave} saving={saving} />, { wrapper });
  return { onSave };
}

beforeEach(() => {
  get.mockReset();
  post.mockReset();
  get.mockResolvedValue({ data: parameters(), error: undefined });
});

describe("TaxParametersSection", () => {
  it("is off by default and explains what turning it on does", async () => {
    renderSection();
    const toggle = screen.getByRole("checkbox", { name: /check for updated irs parameters once a year/i });
    expect(toggle).not.toBeChecked();
    const section = screen.getByTestId("tax-parameters-section");
    expect(section).toHaveTextContent(/off by default/i);
    expect(section).toHaveTextContent(/at most once per calendar year/i);
    expect(section).toHaveTextContent(/no personal data/i);
    expect(screen.getByRole("button", { name: "Save tax settings" })).toBeDisabled();
    await screen.findByTestId("tax-parameters-status");
  });

  it("saves the toggle through onSave", async () => {
    const { onSave } = renderSection();
    await userEvent.click(screen.getByRole("checkbox", { name: /once a year/i }));
    await userEvent.click(screen.getByRole("button", { name: "Save tax settings" }));
    expect(onSave).toHaveBeenCalledWith({ auto_update_params: true });
  });

  it("shows the saved setting and can turn it off again", async () => {
    const { onSave } = renderSection({ ...SETTINGS, tax: { auto_update_params: true } });
    const toggle = screen.getByRole("checkbox", { name: /once a year/i });
    expect(toggle).toBeChecked();
    await userEvent.click(toggle);
    await userEvent.click(screen.getByRole("button", { name: "Save tax settings" }));
    expect(onSave).toHaveBeenCalledWith({ auto_update_params: false });
  });

  it("shows the parameter status: source, version, last check, outcome and covered years", async () => {
    get.mockResolvedValue({
      data: parameters({
        source: "cached",
        params_version: 4,
        cached_version: 4,
        last_checked: "2026-03-01",
        last_check_outcome: "updated",
      }),
      error: undefined,
    });
    renderSection();
    const status = await screen.findByTestId("tax-parameters-status");
    expect(status).toHaveTextContent("Downloaded update");
    expect(status).toHaveTextContent("4 (bundled 3, downloaded 4)");
    expect(status).toHaveTextContent("Mar 1, 2026");
    expect(status).toHaveTextContent("updated");
    expect(status).toHaveTextContent("2022-2026");
  });

  it("shows never checked when there is no last check", async () => {
    renderSection();
    const status = await screen.findByTestId("tax-parameters-status");
    expect(status).toHaveTextContent("Bundled with the app");
    expect(status).toHaveTextContent("Never");
  });

  it("checks now, shows the outcome and refreshes the status", async () => {
    const updated = parameters({
      source: "cached",
      params_version: 4,
      cached_version: 4,
      last_checked: "2026-10-30",
      last_check_outcome: "updated",
    });
    post.mockImplementation(() => {
      // The server now has the new file: later reads (the refetch after the check) report it.
      get.mockResolvedValue({ data: updated, error: undefined });
      return Promise.resolve({
        data: { outcome: "updated", detail: "Updated to parameter version 4.", parameters: updated },
        error: undefined,
      });
    });
    renderSection();
    await screen.findByTestId("tax-parameters-status");
    await userEvent.click(screen.getByRole("button", { name: "Check now" }));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/tax/parameters/refresh"));
    const result = await screen.findByTestId("tax-parameters-refresh-result");
    expect(result).toHaveTextContent("Updated. Updated to parameter version 4.");
    await waitFor(() =>
      expect(within(screen.getByTestId("tax-parameters-status")).getByText("Downloaded update")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("tax-parameters-status")).toHaveTextContent("Oct 30, 2026");
  });

  it("reports an unreachable GitHub without changing anything", async () => {
    post.mockResolvedValue({
      data: {
        outcome: "unreachable",
        detail: "Could not download the file (ConnectError).",
        parameters: parameters({ last_checked: "2026-10-30", last_check_outcome: "unreachable" }),
      },
      error: undefined,
    });
    renderSection();
    await userEvent.click(await screen.findByRole("button", { name: "Check now" }));
    const result = await screen.findByTestId("tax-parameters-refresh-result");
    expect(result).toHaveTextContent("Could not reach GitHub. Could not download the file (ConnectError).");
    expect(result).toHaveClass("text-destructive");
    expect(screen.getByTestId("tax-parameters-status")).toHaveTextContent("Bundled with the app");
  });

  it("shows a failed refresh request and a failed status load", async () => {
    get.mockResolvedValue({ data: undefined, error: { detail: "nope" } });
    post.mockResolvedValue({ data: undefined, error: { detail: "server error" } });
    renderSection();
    expect(await screen.findByText(/Could not load the tax parameter status/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Check now" }));
    expect(await screen.findByText(/Could not check for updates/)).toBeInTheDocument();
  });
});
