import * as React from "react";

import { SegmentedControl, type SegmentedOption } from "./segmented-control";

export type ChartType = "bar" | "line" | "pie";

const LABELS: Record<ChartType, string> = { bar: "Bar", line: "Line", pie: "Pie" };

function Icon({ children }: { children: React.ReactNode }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 16 16"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {children}
    </svg>
  );
}

const ICONS: Record<ChartType, React.ReactNode> = {
  bar: (
    <Icon>
      <path d="M3 13V8M8 13V3M13 13V6" />
    </Icon>
  ),
  line: (
    <Icon>
      <path d="M2 12l4-5 3 3 5-7" />
    </Icon>
  ),
  pie: (
    <Icon>
      <path d="M8 2v6h6" />
      <circle cx="8" cy="8" r="6" />
    </Icon>
  ),
};

export interface ChartTypeToggleProps<T extends ChartType> {
  /** The card's name, used in the group label: "Chart type for <chartLabel>". */
  chartLabel: string;
  value: T;
  options: readonly T[];
  onChange: (value: T) => void;
}

/** Bar / Line / Pie switcher for a chart card header. Labels collapse to icons below `sm`. */
export function ChartTypeToggle<T extends ChartType>({
  chartLabel,
  value,
  options,
  onChange,
}: ChartTypeToggleProps<T>) {
  const items: SegmentedOption<T>[] = options.map((type) => ({
    value: type,
    label: LABELS[type],
    icon: ICONS[type],
    hideLabel: "below-sm",
  }));
  return (
    <SegmentedControl
      label={`Chart type for ${chartLabel}`}
      value={value}
      options={items}
      onChange={onChange}
      size="sm"
    />
  );
}
