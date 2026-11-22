import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

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
});
