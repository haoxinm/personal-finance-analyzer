import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { ThemeToggle } from "./theme-toggle";

describe("ThemeToggle", () => {
  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.classList.remove("dark");
  });

  it("switches to dark and stores it, then back to system", () => {
    render(<ThemeToggle />);
    expect(screen.getByRole("radiogroup", { name: "Colour theme" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: "Dark" }));
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(window.localStorage.getItem("pfa:theme")).toBe("dark");
    expect(screen.getByRole("radio", { name: "Dark" })).toHaveAttribute("aria-checked", "true");

    fireEvent.click(screen.getByRole("radio", { name: "Light" }));
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(window.localStorage.getItem("pfa:theme")).toBe("light");

    fireEvent.click(screen.getByRole("radio", { name: "Match system" }));
    expect(window.localStorage.getItem("pfa:theme")).toBeNull();
    expect(screen.getByRole("radio", { name: "Match system" })).toHaveAttribute("aria-checked", "true");
  });
});
