import { SegmentedControl, type SegmentedOption } from "@/components/primitives/segmented-control";
import { useTheme } from "@/hooks/use-theme";
import type { ThemePreference } from "@/lib/theme";

const ICON = {
  width: 16,
  height: 16,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
};

const OPTIONS: readonly SegmentedOption<ThemePreference>[] = [
  {
    value: "light",
    label: "Light",
    hideLabel: "always",
    icon: (
      <svg {...ICON}>
        <circle cx="12" cy="12" r="4" />
        <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
      </svg>
    ),
  },
  {
    value: "dark",
    label: "Dark",
    hideLabel: "always",
    icon: (
      <svg {...ICON}>
        <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
      </svg>
    ),
  },
  {
    value: "system",
    label: "Match system",
    hideLabel: "always",
    icon: (
      <svg {...ICON}>
        <rect x="2" y="3" width="20" height="14" rx="2" />
        <path d="M8 21h8M12 17v4" />
      </svg>
    ),
  },
];

const CYCLE: readonly ThemePreference[] = ["light", "dark", "system"];

/**
 * Header control for the colour theme. From `sm` up: a Light / Dark / Match system segmented
 * control. Below `sm` (phones) a single icon button that cycles through the three, so the header
 * keeps its room for the navigation.
 */
export function ThemeToggle() {
  const { preference, setPreference } = useTheme();
  const current = OPTIONS.find((o) => o.value === preference) ?? OPTIONS[2]!;
  const next = CYCLE[(CYCLE.indexOf(preference) + 1) % CYCLE.length]!;
  const nextLabel = OPTIONS.find((o) => o.value === next)!.label;
  return (
    <>
      <SegmentedControl
        label="Colour theme"
        value={preference}
        options={OPTIONS}
        onChange={setPreference}
        className="hidden sm:inline-flex"
      />
      <button
        type="button"
        onClick={() => setPreference(next)}
        aria-label={`Colour theme: ${current.label}. Switch to ${nextLabel}`}
        title={`Colour theme: ${current.label}`}
        data-testid="theme-cycle"
        className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-input text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:hidden"
      >
        {current.icon}
      </button>
    </>
  );
}
