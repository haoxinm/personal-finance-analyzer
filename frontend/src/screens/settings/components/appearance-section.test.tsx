import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { initTheme } from "@/lib/theme";
import { AppearanceSection } from "./appearance-section";

describe("AppearanceSection", () => {
  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.classList.remove("dark");
  });

  it("offers Light, Dark and Match my system, and applies the choice", () => {
    render(<AppearanceSection />);
    expect(screen.getByRole("heading", { name: "Appearance" })).toBeInTheDocument();
    expect(screen.getByText("Saved in this browser only.")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Match my system \(currently (Light|Dark)\)/ })).toBeChecked();
    fireEvent.click(screen.getByRole("radio", { name: "Dark" }));
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(window.localStorage.getItem("pfa:theme")).toBe("dark");
    expect(screen.getByRole("radio", { name: "Dark" })).toBeChecked();
  });

  it("reports the OS preference, not the chosen theme, and follows it live", () => {
    let matches = true;
    const listeners = new Set<() => void>();
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({
        get matches() {
          return matches;
        },
        addEventListener: (_: string, l: () => void) => listeners.add(l),
        removeEventListener: (_: string, l: () => void) => listeners.delete(l),
      })),
    );
    window.localStorage.setItem("pfa:theme", "light");
    initTheme();
    render(<AppearanceSection />);
    expect(screen.getByRole("radio", { name: "Match my system (currently Dark)" })).toBeInTheDocument();
    act(() => {
      matches = false;
      listeners.forEach((l) => l());
    });
    expect(screen.getByRole("radio", { name: "Match my system (currently Light)" })).toBeInTheDocument();
    vi.unstubAllGlobals();
  });
});
