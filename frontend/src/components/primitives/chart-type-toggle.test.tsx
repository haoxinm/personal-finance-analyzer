import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ChartTypeToggle } from "./chart-type-toggle";

describe("ChartTypeToggle", () => {
  it("renders one radio per option inside a labelled group", () => {
    render(<ChartTypeToggle chartLabel="Trend" value="line" options={["line", "bar"]} onChange={() => {}} />);
    expect(screen.getByRole("radiogroup", { name: "Chart type for Trend" })).toBeInTheDocument();
    expect(screen.getAllByRole("radio")).toHaveLength(2);
    expect(screen.getByRole("radio", { name: "Line" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "Bar" })).toHaveAttribute("aria-checked", "false");
    expect(screen.queryByRole("radio", { name: "Pie" })).not.toBeInTheDocument();
  });

  it("calls onChange with the chosen type", async () => {
    const onChange = vi.fn();
    render(
      <ChartTypeToggle chartLabel="Spending by category" value="bar" options={["bar", "line", "pie"]} onChange={onChange} />,
    );
    await userEvent.click(screen.getByRole("radio", { name: "Pie" }));
    expect(onChange).toHaveBeenCalledWith("pie");
  });
});
