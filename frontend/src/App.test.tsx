import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

function LocationProbe() {
  const { pathname, search } = useLocation();
  return <div data-testid="location">{pathname + search}</div>;
}

/** Every route's own data hooks run against this one blanket mock — only the route that
 * actually matches ever mounts (`react-router` renders one `Route` element at a time), so this
 * never needs to be more specific than "anything under /api gets an empty, well-typed body". */
async function renderApp(path: string) {
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>(() => Promise.resolve(jsonResponse([]))),
  );
  vi.resetModules();
  const [{ App }] = await Promise.all([import("./App")]);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        initialEntries={[path]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <App />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function navLinkClassName(name: string, scope: HTMLElement = screen.getByRole("navigation", { name: "Main" })): string {
  return within(scope).getByRole("link", { name }).className;
}

const mainNav = () => screen.getByRole("navigation", { name: "Main" });
const sectionNav = (section: string) => screen.getByRole("navigation", { name: `${section} sections` });
const tabNames = (nav: HTMLElement) => within(nav).getAllByRole("link").map((a) => a.textContent);
const activeTabs = (nav: HTMLElement) =>
  within(nav)
    .getAllByRole("link")
    .filter((a) => a.getAttribute("aria-current") === "page")
    .map((a) => a.textContent);

describe("App shell nav", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("lists Spending, Income & tax and Net worth, with Settings pushed right", async () => {
    await renderApp("/spending");
    expect(tabNames(mainNav())).toEqual(["Spending", "Income & tax", "Net worth", "Settings"]);
    expect(screen.getByRole("link", { name: "Settings" }).className).toContain("ml-auto");
    expect(screen.queryByRole("link", { name: "Component gallery" })).not.toBeInTheDocument();
  });

  it("renders a 'Page not found' page with links to the sections for an unknown route", async () => {
    await renderApp("/no/such/page");
    const page = await screen.findByTestId("not-found");
    expect(within(page).getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    const links = within(page).getAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual(["Spending", "Income & tax", "Net worth", "Settings"]);
    expect(links[0]).toHaveAttribute("href", "/spending");
    // Known routes are unaffected.
    expect(screen.getByTestId("location")).toHaveTextContent("/no/such/page");
  });

  it("redirects '/' to Spending with Overview active", async () => {
    await renderApp("/");
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/spending"));
    expect(screen.getByTestId("location")).not.toHaveTextContent("/spending/");
    expect(navLinkClassName("Spending")).toContain("bg-muted");
    expect(navLinkClassName("Income & tax")).not.toContain("bg-muted");
    expect(activeTabs(sectionNav("Spending"))).toEqual(["Overview"]);
  });

  it("shows the active section's sub-tabs, without the hidden Layout mapper", async () => {
    await renderApp("/spending/review");
    expect(tabNames(sectionNav("Spending"))).toEqual(["Overview", "Transactions", "Review", "Import", "Rules"]);
    expect(activeTabs(sectionNav("Spending"))).toEqual(["Review"]);
    expect(screen.queryByRole("navigation", { name: "Income & tax sections" })).not.toBeInTheDocument();
  });

  it("switches the sub-tab row with the section", async () => {
    await renderApp("/tax/documents");
    expect(navLinkClassName("Income & tax")).toContain("bg-muted");
    expect(navLinkClassName("Spending")).not.toContain("bg-muted");
    expect(tabNames(sectionNav("Income & tax"))).toEqual(["Overview", "Estimate", "Documents", "Import"]);
    expect(activeTabs(sectionNav("Income & tax"))).toEqual(["Documents"]);
  });

  it("puts Estimate between Overview and Documents and marks it active on its route", async () => {
    await renderApp("/tax/estimate?year=2023");
    expect(activeTabs(sectionNav("Income & tax"))).toEqual(["Estimate"]);
    expect(document.title).toBe("Estimate · Income & tax · Personal Finance Analyzer");
    expect(within(sectionNav("Income & tax")).getByRole("link", { name: "Overview" })).toHaveAttribute(
      "href",
      "/tax?year=2023",
    );
  });

  it("has no sub-tab row for the single-page sections", async () => {
    await renderApp("/settings");
    expect(navLinkClassName("Settings")).toContain("bg-muted");
    expect(screen.queryByRole("navigation", { name: /sections$/ })).not.toBeInTheDocument();
  });

  it("splits Net worth into Overview and Forecast sub-tabs", async () => {
    await renderApp("/net-worth/forecast");
    expect(navLinkClassName("Net worth")).toContain("bg-muted");
    expect(tabNames(sectionNav("Net worth"))).toEqual(["Overview", "Forecast"]);
    expect(activeTabs(sectionNav("Net worth"))).toEqual(["Forecast"]);
    expect(document.title).toBe("Forecast · Net worth · Personal Finance Analyzer");
    // The Overview tab is `end`-matched, so it is not also active on the Forecast route.
    expect(within(mainNav()).getByRole("link", { name: "Net worth" })).toHaveAttribute("href", "/net-worth");
  });

  /**
   * Regression coverage for a real bug: Layout mapper is a deep-link-only screen with no tab of
   * its own, and a stale highlight was once seen there (Settings). Now it belongs to Spending:
   * that section is active, but none of its sub-tabs are, and nothing else is.
   * "bg-muted" only ever appears together with the active state (see `NavItem` / `SectionTabs`):
   * `hover:text-foreground` is present on every link regardless, so it isn't a useful signal.
   */
  it("marks Spending active but no sub-tab on the Layout mapper", async () => {
    await renderApp("/spending/layout-mapper");
    expect(navLinkClassName("Spending")).toContain("bg-muted");
    for (const name of ["Income & tax", "Net worth", "Settings"]) {
      expect(navLinkClassName(name)).not.toContain("bg-muted");
    }
    const tabs = sectionNav("Spending");
    for (const name of tabNames(tabs)) {
      expect(navLinkClassName(name as string, tabs)).not.toContain("bg-muted");
    }
    expect(activeTabs(tabs)).toEqual([]);
  });

  it("sets the document title from the route", async () => {
    await renderApp("/spending/review");
    expect(document.title).toBe("Review · Spending · Personal Finance Analyzer");
  });

  it("carries ?compare= only to compare-capable links", async () => {
    await renderApp("/spending?compare=2");
    const tabs = sectionNav("Spending");
    expect(within(tabs).getByRole("link", { name: "Review" })).toHaveAttribute("href", "/spending/review?compare=2");
    expect(within(tabs).getByRole("link", { name: "Rules" })).toHaveAttribute("href", "/spending/rules");
    expect(within(tabs).getByRole("link", { name: "Import" })).toHaveAttribute("href", "/spending/import");
    expect(within(mainNav()).getByRole("link", { name: "Income & tax" })).toHaveAttribute("href", "/tax?compare=2");
    expect(within(mainNav()).getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
  });

  it("carries ?year= between the Income & tax tabs, but not into other sections", async () => {
    await renderApp("/tax?year=2023");
    const tabs = sectionNav("Income & tax");
    expect(within(tabs).getByRole("link", { name: "Estimate" })).toHaveAttribute("href", "/tax/estimate?year=2023");
    expect(within(tabs).getByRole("link", { name: "Documents" })).toHaveAttribute("href", "/tax/documents?year=2023");
    expect(within(tabs).getByRole("link", { name: "Import" })).toHaveAttribute("href", "/tax/import?year=2023");
    expect(within(mainNav()).getByRole("link", { name: "Net worth" })).toHaveAttribute("href", "/net-worth");
  });
});

describe("page headings", () => {
  beforeEach(() => {
    vi.resetModules();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  // Every screen renders its one h1 through PageHeader, in its loading and error states too (the
  // blanket mock answers every query with `[]`, which several screens treat as an error).
  it.each([
    ["/spending", "Spending overview"],
    ["/spending/transactions", "Transactions"],
    ["/spending/review", "Review transactions"],
    ["/spending/import", "Import statements"],
    ["/spending/rules", "Rules & taxonomy"],
    ["/spending/layout-mapper", "Layout mapper"],
    ["/tax", "Income & tax overview"],
    ["/tax/estimate", "Tax estimate"],
    ["/tax/documents", "Tax documents"],
    ["/tax/import", "Import tax forms"],
    ["/net-worth", "Net worth overview"],
    ["/net-worth/forecast", "Net worth forecast"],
    ["/settings", "Settings"],
  ])("%s has a single h1 named %s", async (path, title) => {
    await renderApp(path);
    const main = screen.getByRole("main");
    expect(await within(main).findByRole("heading", { level: 1, name: title })).toBeInTheDocument();
    expect(within(main).getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });
});

describe("legacy paths", () => {
  beforeEach(() => {
    vi.resetModules();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it.each([
    ["/import?user=2", "/spending/import?user=2"],
    ["/review?user=1&compare=2", "/spending/review?user=1&compare=2"],
    ["/transactions?user=2&category_keys=dining", "/spending/transactions?user=2&category_keys=dining"],
    ["/rules?tab=taxonomy", "/spending/rules?tab=taxonomy"],
    ["/layout-mapper?statement_id=42", "/spending/layout-mapper?statement_id=42"],
    ["/dashboard?currency=EUR", "/spending?currency=EUR"],
    ["/?user=2&compare=1", "/spending?user=2&compare=1"],
  ])("redirects %s to %s", async (from, to) => {
    await renderApp(from);
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(`${to}`));
    expect(screen.getByTestId("location").textContent).toBe(to);
  });
});
