import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Settings } from "../lib/hooks";
import { FolderImportsSection } from "./folder-imports-section";

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
  watch: { enabled: false, interval_seconds: 60, watched_folder: "/pfa/documents" },
  env_overrides: [],
};

function renderSection(settings: Settings = SETTINGS) {
  const onSave = vi.fn();
  render(<FolderImportsSection settings={settings} onSave={onSave} saving={false} />);
  return { onSave };
}

describe("FolderImportsSection", () => {
  it("is off by default and shows the watched folder read-only", () => {
    renderSection();
    expect(screen.getByLabelText(/Import new PDFs from the documents folder automatically/)).not.toBeChecked();
    expect(screen.getByTestId("folder-imports-folder")).toHaveTextContent("/pfa/documents");
    expect(screen.queryByRole("textbox", { name: /documents folder/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save folder imports" })).toBeDisabled();
  });

  it("saves the toggle and the interval", async () => {
    const { onSave } = renderSection();
    await userEvent.click(screen.getByLabelText(/Import new PDFs from the documents folder automatically/));
    const interval = screen.getByLabelText(/Check the folder every/);
    await userEvent.clear(interval);
    await userEvent.type(interval, "120");
    await userEvent.click(screen.getByRole("button", { name: "Save folder imports" }));
    expect(onSave).toHaveBeenCalledWith({ enabled: true, interval_seconds: 120, watched_folder: "/pfa/documents" });
  });

  it("refuses an interval outside 10 to 86400 seconds", async () => {
    renderSection();
    const interval = screen.getByLabelText(/Check the folder every/);
    await userEvent.clear(interval);
    await userEvent.type(interval, "5");
    expect(interval).toHaveAttribute("aria-invalid", "true");
    await userEvent.click(screen.getByLabelText(/Import new PDFs from the documents folder automatically/));
    expect(screen.getByRole("button", { name: "Save folder imports" })).toBeDisabled();
  });

  it("warns plainly that an enabled LLM categorizes new statements without a click", () => {
    renderSection({ ...SETTINGS, llm: { ...SETTINGS.llm, enabled: true, mode: "remote" } });
    expect(
      screen.getByText(/descriptions of new statements are sent to it to be categorized automatically/),
    ).toBeInTheDocument();
  });

  it("still says so when no LLM is on yet", () => {
    renderSection();
    expect(screen.getByText(/If you enable an LLM later/)).toBeInTheDocument();
  });

  it("disables what an environment variable controls and says so", () => {
    renderSection({
      ...SETTINGS,
      watch: { enabled: true, interval_seconds: 30, watched_folder: "/pfa/documents" },
      env_overrides: ["watch.enabled", "watch.interval_seconds"],
    });
    expect(screen.getByLabelText(/Import new PDFs from the documents folder automatically/)).toBeDisabled();
    expect(screen.getByLabelText(/Check the folder every/)).toBeDisabled();
    expect(screen.getByTestId("folder-imports-env")).toHaveTextContent("watch.enabled, watch.interval_seconds");
  });

  it("works against settings from an older server that has no watch section", () => {
    const { watch: _watch, ...older } = SETTINGS;
    void _watch;
    renderSection(older);
    expect(screen.getByLabelText(/Check the folder every/)).toHaveValue(60);
  });
});
