import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setMockHandlers, TestProviders } from "./test-utils";

vi.mock("@/api/client", async () => {
  const { dynamicMockFetch } = await import("./test-utils");
  vi.stubGlobal("fetch", dynamicMockFetch);
  return vi.importActual("@/api/client");
});

describe("RulesScreen", () => {
  beforeEach(() => {
    setMockHandlers({
      "GET /rules": () => [],
      "GET /taxonomy": () => [],
      "GET /transactions": () => ({ items: [], total: 0 }),
      "GET /issuers": () => [],
      "GET /accounts": () => [],
      "GET /statements": () => [],
      "GET /layout-specs": () => [],
    });
  });

  it("exports the screen contract (default component + screenMeta)", async () => {
    const mod = await import("./index");
    expect(mod.screenMeta).toEqual({ path: "/spending/rules", title: "Rules" });
    expect(typeof mod.default).toBe("function");
  });

  it("shows the Rules tab by default and switches to Extractors and Taxonomy", async () => {
    const { default: RulesScreen } = await import("./index");
    const user = userEvent.setup();
    render(<RulesScreen />, { wrapper: TestProviders });

    expect(screen.getByRole("tab", { name: "Rules" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("No rules yet")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Extractors" }));
    expect(screen.getByRole("tab", { name: "Extractors" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("No layout specs yet")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Taxonomy" }));
    expect(screen.getByRole("tab", { name: "Taxonomy" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("No taxonomy loaded")).toBeInTheDocument();
  });
});
