import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

/**
 * D9's Playwright happy path (§2, Phase 3 Gate 3): import the generated `layout_b_credit`
 * fixture, confirm its extractor, land in the review queue, and read a chart on the Spending overview —
 * end to end against the real backend `e2e/global-setup.ts` boots.
 */

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const FIXTURE_PDF = path.resolve(
  __dirname,
  "..",
  "..",
  "tests",
  "fixtures",
  "generated",
  "layout_b_credit",
  "layout_b_credit_normal.pdf",
);

function baseUrl(): string {
  const url = process.env.E2E_BASE_URL;
  if (!url) throw new Error("E2E_BASE_URL not set — global setup did not run");
  return url;
}

test("import a statement, confirm the extractor, review it, and see an overview chart", async ({
  page,
}) => {
  await page.goto(baseUrl());
  await expect(page.getByRole("heading", { name: "Spend Analyzer" }).or(page.locator("body"))).toBeVisible();
  // `/` lands on the Spending section.
  await expect(page).toHaveURL(/\/spending$/);

  // --- Import -------------------------------------------------------------------------------
  await page.getByRole("link", { name: "Import", exact: true }).click();
  await expect(page).toHaveURL(/\/spending\/import/);
  await expect(page.getByRole("heading", { name: "Import statements" })).toBeVisible();

  await page.getByLabel("Choose statement PDFs").setInputFiles(FIXTURE_PDF);

  // The upload landed and proposed the built-in layout_b_credit parser, but no issuer matched
  // yet on this fresh install (§2f.4) — create one inline (§P3-A).
  await page.getByRole("button", { name: "New" }).click();
  await page.getByPlaceholder("Issuer name, e.g. Bank of America").fill("Test Bank");
  await page.getByRole("button", { name: "Add" }).click();

  await page.getByRole("button", { name: "Import" }).click();

  // Extraction, then classification (LLM mode "none" — no network call, §D2), finish and the
  // summary line reports an exact needs-review count (`pollClassifyJobNeedsReview`, §3.12).
  await expect(page.getByRole("status")).toContainText(/statement.*imported/, { timeout: 30_000 });
  await expect(page.getByRole("status")).toContainText(/need review/);

  // --- Review ---------------------------------------------------------------------------------
  await page.getByRole("link", { name: "Review", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Review" })).toBeVisible();
  await expect(page.getByText(/\d+ need review/)).toBeVisible({ timeout: 15_000 });

  // --- Overview -------------------------------------------------------------------------------
  await page.getByRole("link", { name: "Overview", exact: true }).click();
  await expect(page).toHaveURL(/\/spending$/);
  const chart = page.getByTestId("category-stacked-bar");
  await expect(chart).toBeVisible();
  await expect(chart.locator("svg.recharts-surface").first()).toBeVisible();
  await expect(chart.locator("svg path, svg rect")).not.toHaveCount(0);
});
