import { defineConfig, devices } from "@playwright/test";

/**
 * D9's one Playwright happy path (§2, §5 Phase 3 Gate 3): import a generated fixture PDF,
 * confirm the extractor, see the review queue, and read a chart on the Spending overview — against the
 * real backend, booted by `e2e/global-setup.ts` (temp `SPEND_ANALYZER_HOME`, LLM mode "none"),
 * serving whatever is built into `src/spend_analyzer/web/` (`npm run build` first — CI does).
 * `baseURL` is set per-test from `process.env.E2E_BASE_URL`, which global setup reads off the
 * server's own printed URL, so this file hard-codes no port.
 *
 * Chromium is preinstalled at `/opt/pw-browsers` (`PLAYWRIGHT_BROWSERS_PATH` is set in this
 * environment); this project only ever runs `chromium`, so `npx playwright install` is never
 * needed here.
 */
export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [["github"], ["list"]] : "list",
  timeout: 60_000,
  use: {
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
