import { fireEvent, render, screen } from "@testing-library/react";
import * as React from "react";
import { describe, expect, it } from "vitest";

import { SegmentedControl } from "./segmented-control";

const OPTIONS = [
  { value: "a", label: "Alpha" },
  { value: "b", label: "Beta" },
  { value: "c", label: "Gamma", hideLabel: "always" as const },
];

function Harness({ initial = "a" }: { initial?: string }) {
  const [value, setValue] = React.useState(initial);
  return <SegmentedControl label="Pick one" value={value} options={OPTIONS} onChange={setValue} />;
}

describe("SegmentedControl", () => {
  it("is a labelled radiogroup with aria-checked on the current option", () => {
    render(<Harness initial="b" />);
    expect(screen.getByRole("radiogroup", { name: "Pick one" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Beta" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "Alpha" })).toHaveAttribute("aria-checked", "false");
  });

  it("only makes the checked option tabbable", () => {
    render(<Harness />);
    expect(screen.getByRole("radio", { name: "Alpha" })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("radio", { name: "Beta" })).toHaveAttribute("tabindex", "-1");
  });

  it("moves and selects with the arrow, Home and End keys", () => {
    render(<Harness />);
    const alpha = screen.getByRole("radio", { name: "Alpha" });
    fireEvent.keyDown(alpha, { key: "ArrowRight" });
    expect(screen.getByRole("radio", { name: "Beta" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "Beta" })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("radio", { name: "Beta" }), { key: "End" });
    expect(screen.getByRole("radio", { name: "Gamma" })).toHaveAttribute("aria-checked", "true");
    fireEvent.keyDown(screen.getByRole("radio", { name: "Gamma" }), { key: "ArrowRight" });
    expect(screen.getByRole("radio", { name: "Alpha" })).toHaveAttribute("aria-checked", "true");
    fireEvent.keyDown(screen.getByRole("radio", { name: "Alpha" }), { key: "ArrowLeft" });
    expect(screen.getByRole("radio", { name: "Gamma" })).toHaveAttribute("aria-checked", "true");
    fireEvent.keyDown(screen.getByRole("radio", { name: "Gamma" }), { key: "Home" });
    expect(screen.getByRole("radio", { name: "Alpha" })).toHaveAttribute("aria-checked", "true");
  });

  it("selects on click", () => {
    render(<Harness />);
    fireEvent.click(screen.getByRole("radio", { name: "Beta" }));
    expect(screen.getByRole("radio", { name: "Beta" })).toHaveAttribute("aria-checked", "true");
  });
});
