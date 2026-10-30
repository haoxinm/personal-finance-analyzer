import { Money } from "@/components/primitives/money";

import { MoneyField } from "./fields";
import { yearsToGoal, type DollarMode, type ForecastInputs, type ForecastRow, type ForecastSummary } from "./forecast-logic";

export interface SummaryCardsProps {
  inputs: ForecastInputs;
  rows: readonly ForecastRow[];
  summary: ForecastSummary;
  mode: DollarMode;
  goalMinor: number | null;
  onGoalChange: (goalMinor: number | null) => void;
}

const TILE = "rounded-lg border border-border p-4";
const TILE_LABEL = "text-xs font-medium text-muted-foreground";

/** Headline figures for the target year: net worth (nominal and today's dollars), how much of
 * the change came from saving versus investment growth, and when an optional goal is reached. */
export function SummaryCards({ inputs, rows, summary, mode, goalMinor, onGoalChange }: SummaryCardsProps) {
  const goal = goalMinor !== null && goalMinor > 0 ? yearsToGoal(inputs, rows, goalMinor, mode) : null;
  const goalSet = goalMinor !== null && goalMinor > 0;
  const total = summary.contributions + summary.growth;
  const growthShare = total !== 0 ? Math.round((summary.growth / total) * 100) : null;

  return (
    <div className="flex flex-col gap-3" data-testid="forecast-summary">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div className={TILE}>
          <p className={TILE_LABEL}>Net worth in {summary.targetYear}</p>
          <p className="mt-1 text-2xl font-semibold" data-testid="forecast-nominal">
            <Money tone="neutral" minorUnits={Math.round(summary.netWorthNominal)} />
          </p>
          <p className="mt-1 text-xs text-muted-foreground">Nominal dollars</p>
        </div>
        <div className={TILE}>
          <p className={TILE_LABEL}>In today&apos;s dollars</p>
          <p className="mt-1 text-2xl font-semibold" data-testid="forecast-real">
            <Money tone="neutral" minorUnits={Math.round(summary.netWorthReal)} />
          </p>
          <p className="mt-1 text-xs text-muted-foreground">Adjusted for inflation to {inputs.baseYear}</p>
        </div>
        <div className={TILE}>
          <p className={TILE_LABEL}>Saved and one-off events</p>
          <p className="mt-1 text-2xl font-semibold" data-testid="forecast-contributions">
            <Money tone="neutral" minorUnits={Math.round(summary.contributions)} />
          </p>
          <p className="mt-1 text-xs text-muted-foreground">Total over the forecast</p>
        </div>
        <div className={TILE}>
          <p className={TILE_LABEL}>Investment growth</p>
          <p className="mt-1 text-2xl font-semibold" data-testid="forecast-growth">
            <Money tone="neutral" minorUnits={Math.round(summary.growth)} />
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            {growthShare !== null ? `${growthShare}% of the total change` : "Total over the forecast"}
          </p>
        </div>
      </div>
      <div className={`${TILE} flex flex-wrap items-end gap-4`}>
        <MoneyField
          label={`Goal (${mode === "real" ? "today's dollars" : "nominal dollars"})`}
          value={goalMinor}
          nullable
          placeholder="e.g. 1,000,000"
          onChange={onGoalChange}
          className="w-52"
        />
        <p className="pb-2 text-sm" data-testid="forecast-goal-result" aria-live="polite">
          {!goalSet
            ? "Enter an amount to see when you could reach it."
            : goal === null
              ? `Not reached by ${summary.targetYear}.`
              : goal.years === 0
                ? "Already reached at the start."
                : `Reached in ${goal.year} (${goal.years} year${goal.years === 1 ? "" : "s"} from ${inputs.baseYear}).`}
        </p>
      </div>
    </div>
  );
}
