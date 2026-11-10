import * as React from "react";

import { Page, PageHeader } from "@/components/primitives/page";
import { EmptyState, ErrorState, LoadingState } from "@/components/primitives/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useSelectedUser } from "@/hooks/use-selected-user";
import { cn } from "@/utils";

import { useForecastBaseline, useForecastPresets } from "./api";
import { ForecastChart } from "./forecast-chart";
import { project, rowsIn, summarize, type DollarMode } from "./forecast-logic";
import { ForecastTable } from "./forecast-table";
import { HistoryEditor } from "./history-editor";
import { ParametersPanel } from "./parameters-panel";
import {
  INITIAL_STATE,
  applyBuiltin,
  applySaved,
  resolveInputs,
  type Baseline,
  type ForecastState,
  type PresetRef,
  type SavedPreset,
} from "./params";
import { PresetBar } from "./preset-bar";
import { SummaryCards } from "./summary-cards";

const MODERATE: PresetRef = { kind: "builtin", key: "moderate" };

/**
 * The Net worth forecast: a deterministic, illustrative projection of net worth to a chosen year,
 * from the user's own history (balance entries, tax forms, spending) and adjustable assumptions.
 * The projection is computed in the browser as the inputs change; the server supplies history and
 * stores manual past years and saved presets. One person (or everyone combined) at a time, so it
 * is not compare-capable.
 */
export default function ForecastScreen() {
  const sel = useSelectedUser();
  return (
    <Page>
      <PageHeader
        title="Net worth forecast"
        description="Project net worth to a future year from your history and the assumptions below."
      />
      <p role="note" className="rounded-md border border-border bg-muted/40 p-3 text-sm text-muted-foreground">
        This is an illustrative projection, not financial, tax or investment advice. It is deterministic: it assumes
        every rate stays constant, and real returns, income and spending will vary. All figures are in USD and stay on
        this computer.
      </p>
      {sel.isLoading ? null : <ForecastBody key={sel.selectedUserId ?? "all"} userId={sel.selectedUserId} />}
    </Page>
  );
}

function ForecastBody({ userId }: { userId: number | null }) {
  const baselineQuery = useForecastBaseline(userId);
  const presetsQuery = useForecastPresets(userId);
  const currentYear = React.useMemo(() => new Date().getFullYear(), []);
  const [state, setState] = React.useState<ForecastState>(INITIAL_STATE);
  const [preset, setPreset] = React.useState<PresetRef | null>(MODERATE);
  const [mode, setMode] = React.useState<DollarMode>("nominal");
  const [goal, setGoal] = React.useState<number | null>(null);

  const baseline: Baseline | null | undefined = baselineQuery.data;
  const defaults = baseline?.defaults;
  const resolved = React.useMemo(() => resolveInputs(state, defaults, currentYear), [state, defaults, currentYear]);
  const { inputs } = resolved;
  const rows = React.useMemo(() => project(inputs), [inputs]);
  const shown = React.useMemo(() => rowsIn(mode, rows, inputs.baseYear, inputs.inflationPct), [mode, rows, inputs]);
  const summary = React.useMemo(() => summarize(inputs, rows), [inputs, rows]);

  const builtin = presetsQuery.data?.builtin ?? [];
  const saved = presetsQuery.data?.saved ?? [];
  const invalid = presetsQuery.data?.invalid ?? [];
  const patch = (p: Partial<ForecastState>) => setState((s) => ({ ...s, ...p }));

  function selectPreset(ref: PresetRef | null) {
    setPreset(ref);
    if (ref === null) return;
    if (ref.kind === "builtin") {
      const p = builtin.find((b) => b.key === ref.key);
      if (p) setState((s) => applyBuiltin(s, p.params));
    } else {
      const p = saved.find((x) => x.id === ref.id);
      if (p) setState((s) => applySaved(s, p.params, defaults?.net_worth_year ?? currentYear));
    }
  }
  const onSaved = (p: SavedPreset) => setPreset({ kind: "saved", id: p.id });

  if (baselineQuery.error) {
    const err = baselineQuery.error;
    return (
      <ErrorState
        title="Could not load your history"
        description={err instanceof Error ? err.message : String(err)}
        onRetry={() => void baselineQuery.refetch()}
      />
    );
  }
  if (baselineQuery.isLoading) return <LoadingState rows={4} />;

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]" data-testid="forecast-screen">
      <div className="flex flex-col gap-4">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Scenario</CardTitle>
          </CardHeader>
          <CardContent>
            <PresetBar
              userId={userId}
              builtin={builtin}
              saved={saved}
              invalid={invalid}
              selected={preset}
              state={state}
              resolved={resolved}
              onSelect={selectPreset}
              onSaved={onSaved}
            />
            {presetsQuery.error ? (
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <p role="alert" className="text-xs text-destructive">
                  Could not load presets.
                </p>
                <Button size="sm" variant="outline" onClick={() => void presetsQuery.refetch()}>
                  Try again
                </Button>
              </div>
            ) : null}
          </CardContent>
        </Card>
        <ParametersPanel
          state={state}
          onChange={patch}
          defaults={defaults}
          resolved={resolved}
          onResetAll={() => {
            setState(INITIAL_STATE);
            setPreset(MODERATE);
          }}
        />
      </div>

      <div className="flex min-w-0 flex-col gap-4">
        {resolved.ready ? (
          <>
            <SummaryCards inputs={inputs} rows={rows} summary={summary} mode={mode} goalMinor={goal} onGoalChange={setGoal} />
            <Card>
              <CardHeader className="flex-row flex-wrap items-center justify-between gap-3">
                <CardTitle className="text-base">Net worth to {summary.targetYear}</CardTitle>
                <DollarToggle mode={mode} onChange={setMode} baseYear={inputs.baseYear} />
              </CardHeader>
              <CardContent>
                <ForecastChart
                  history={baseline?.history ?? []}
                  rows={shown}
                  baseYear={inputs.baseYear}
                  startNetWorthMinor={inputs.startNetWorthMinor}
                  targetYear={summary.targetYear}
                  mode={mode}
                />
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Year by year</CardTitle>
              </CardHeader>
              <CardContent>
                <ForecastTable rows={shown} mode={mode} baseYear={inputs.baseYear} />
              </CardContent>
            </Card>
          </>
        ) : (
          <EmptyState
            title="Enter a starting net worth"
            description="There is no net worth history yet. Type a starting net worth under Assumptions (income and spending too, if you like), add balances on the Overview tab, or enter a past year below."
          />
        )}
        <HistoryEditor userId={userId} history={baseline?.history ?? []} />
      </div>
    </div>
  );
}

function DollarToggle({
  mode,
  onChange,
  baseYear,
}: {
  mode: DollarMode;
  onChange: (m: DollarMode) => void;
  baseYear: number;
}) {
  // "Real" values are in starting-year dollars (the model's base year), not necessarily today's.
  const options: Array<{ value: DollarMode; label: string }> = [
    { value: "nominal", label: "Nominal" },
    { value: "real", label: `${baseYear} dollars` },
  ];
  return (
    <div role="radiogroup" aria-label="Dollars" className="inline-flex gap-1 rounded-full bg-muted p-1">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={mode === o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            "rounded-full border px-3 py-1 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            mode === o.value
              ? "border-primary bg-primary text-primary-foreground shadow-sm"
              : "border-transparent text-muted-foreground hover:text-foreground",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export const screenMeta = { path: "/net-worth/forecast", title: "Net worth forecast" };
