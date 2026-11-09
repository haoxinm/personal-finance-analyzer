import * as React from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { MAX_HORIZON_YEARS } from "./forecast-logic";
import { MoneyField, PercentField, YearField } from "./fields";
import {
  MAX_EVENTS,
  MAX_YEAR,
  MIN_YEAR,
  formatPct,
  formatUsd,
  type Defaults,
  type ForecastState,
  type Resolved,
} from "./params";

export interface ParametersPanelProps {
  state: ForecastState;
  onChange: (patch: Partial<ForecastState>) => void;
  /** History-derived suggestions; undefined while loading or with no data. */
  defaults: Defaults | undefined;
  resolved: Pick<Resolved, "baseYear" | "targetYear" | "data">;
  onResetAll: () => void;
}

const SECTION = "flex flex-col gap-3 border-t border-border pt-4 first:border-t-0 first:pt-0";
const SECTION_TITLE = "text-sm font-medium";

/** Every adjustable assumption, each with the figure history suggests and a "Reset to data"
 * action, plus the one-off events list. Changes apply immediately (the projection is computed in
 * the browser). */
export function ParametersPanel({ state, onChange, defaults, resolved, onResetAll }: ParametersPanelProps) {
  const d = defaults;
  const base = resolved.baseYear;
  const netWorthText =
    d?.net_worth_minor != null
      ? `Data: ${formatUsd(d.net_worth_minor)}${d.net_worth_year ? ` (${d.net_worth_year})` : ""}` +
        (d.net_worth_year && d.net_worth_year !== base
          ? `. Not adjusted: the starting year is ${base}, so check this figure is as of ${base}.`
          : "")
      : null;
  const incomeText =
    d?.annual_income_minor != null
      ? `Data: ${formatUsd(d.annual_income_minor)}${d.income_year ? ` (${d.income_year}, wages and self-employment from tax forms)` : ""}` +
        rebasedNote(d.income_year, base, resolved.data.incomeMinor, state.incomeGrowthPct, "income growth")
      : null;
  const spendingText =
    d?.annual_spending_minor != null
      ? `Data: ${formatUsd(d.annual_spending_minor)}${d.spending_year ? ` (${d.spending_year}${d.spending_annualized ? ", scaled up from a partial year" : ""})` : ""}` +
        rebasedNote(d.spending_year, base, resolved.data.spendingMinor, state.spendingGrowthPct, "spending growth")
      : null;
  const observed = (v: number | null | undefined, clamped: boolean | undefined) =>
    v != null
      ? `Observed: ${formatPct(v)}/yr` +
        (clamped ? " (capped: the raw figure was outside -50% to 50%, often a partial first year; check your data)" : "")
      : null;
  const clampPct = (v: number) => Math.min(Math.max(v, -50), 50);

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-3">
        <CardTitle className="text-base">Assumptions</CardTitle>
        <Button type="button" variant="ghost" size="sm" onClick={onResetAll}>
          Reset all
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <section aria-label="Timeline" className={SECTION}>
          <h3 className={SECTION_TITLE}>Timeline</h3>
          <YearField
            label="Forecast to year"
            value={resolved.targetYear}
            min={base + 1}
            max={base + MAX_HORIZON_YEARS}
            onChange={(v) => onChange({ targetYear: v })}
            hint={`${resolved.targetYear - base} year${resolved.targetYear - base === 1 ? "" : "s"} after ${base}; at most ${MAX_HORIZON_YEARS}.`}
          />
          <YearField
            label="Starting year"
            value={state.baseYear}
            min={MIN_YEAR}
            max={MAX_YEAR}
            nullable
            placeholder={String(base)}
            onChange={(v) => onChange({ baseYear: v })}
            hint="The year the starting net worth is as of. Empty uses the latest year with data."
          />
          <MoneyField
            label="Starting net worth"
            value={state.startNetWorthMinor}
            nullable
            allowNegative
            placeholder={d?.net_worth_minor != null ? (d.net_worth_minor / 100).toFixed(2) : "0.00"}
            dataText={netWorthText}
            onChange={(v) => onChange({ startNetWorthMinor: v })}
            onReset={() => onChange({ startNetWorthMinor: null })}
          />
        </section>

        <section aria-label="Income" className={SECTION}>
          <h3 className={SECTION_TITLE}>Income</h3>
          <MoneyField
            label="Annual earned income"
            value={state.annualIncomeMinor}
            nullable
            placeholder={resolved.data.incomeMinor != null ? (resolved.data.incomeMinor / 100).toFixed(2) : "0.00"}
            dataText={incomeText}
            onChange={(v) => onChange({ annualIncomeMinor: v })}
            onReset={() => onChange({ annualIncomeMinor: null })}
          />
          <PercentField
            label="Income growth per year"
            value={state.incomeGrowthPct}
            min={-50}
            max={50}
            slider={{ min: -5, max: 15 }}
            dataText={observed(d?.income_growth_pct, d?.income_growth_clamped)}
            canReset={d?.income_growth_pct != null && state.incomeGrowthPct !== d.income_growth_pct}
            onChange={(v) => v !== null && onChange({ incomeGrowthPct: v })}
            onReset={() => d?.income_growth_pct != null && onChange({ incomeGrowthPct: clampPct(d.income_growth_pct) })}
          />
          <PercentField
            label="Effective tax rate"
            value={state.taxRatePct}
            nullable
            min={0}
            max={100}
            placeholder={d?.effective_tax_rate_pct != null ? String(d.effective_tax_rate_pct) : "0"}
            dataText={
              d?.effective_tax_rate_pct != null
                ? `Data: ${formatPct(d.effective_tax_rate_pct)} (all-in tax / earned income)`
                : state.taxRatePct === null
                  ? "No tax data found, so tax is counted as 0%. Import tax forms or type a rate to avoid overstating savings."
                  : null
            }
            onChange={(v) => onChange({ taxRatePct: v })}
            onReset={() => onChange({ taxRatePct: null })}
          />
        </section>

        <section aria-label="Spending" className={SECTION}>
          <h3 className={SECTION_TITLE}>Spending</h3>
          <MoneyField
            label="Annual spending"
            value={state.annualSpendingMinor}
            nullable
            placeholder={resolved.data.spendingMinor != null ? (resolved.data.spendingMinor / 100).toFixed(2) : "0.00"}
            dataText={spendingText}
            onChange={(v) => onChange({ annualSpendingMinor: v })}
            onReset={() => onChange({ annualSpendingMinor: null })}
          />
          <PercentField
            label="Spending growth per year"
            value={state.spendingGrowthPct}
            min={-50}
            max={50}
            slider={{ min: -2, max: 10 }}
            dataText={observed(d?.spending_growth_pct, d?.spending_growth_clamped)}
            canReset={d?.spending_growth_pct != null && state.spendingGrowthPct !== d.spending_growth_pct}
            onChange={(v) => v !== null && onChange({ spendingGrowthPct: v })}
            onReset={() => d?.spending_growth_pct != null && onChange({ spendingGrowthPct: clampPct(d.spending_growth_pct) })}
          />
          <MoneyField
            label="Fixed change in spending per year"
            value={state.spendingChangeMinor}
            allowNegative
            onChange={(v) => onChange({ spendingChangeMinor: v ?? 0 })}
          />
        </section>

        <section aria-label="Markets" className={SECTION}>
          <h3 className={SECTION_TITLE}>Investments and inflation</h3>
          <PercentField
            label="Investment return per year"
            value={state.returnPct}
            min={-20}
            max={30}
            slider={{ min: -5, max: 15 }}
            onChange={(v) => v !== null && onChange({ returnPct: v })}
          />
          <PercentField
            label="Inflation per year"
            value={state.inflationPct}
            min={-10}
            max={30}
            slider={{ min: 0, max: 10 }}
            onChange={(v) => v !== null && onChange({ inflationPct: v })}
          />
        </section>

        <section aria-label="Retirement" className={SECTION}>
          <h3 className={SECTION_TITLE}>Retirement</h3>
          <YearField
            label="Last working year"
            value={state.retirementYear}
            min={MIN_YEAR}
            max={MAX_YEAR}
            nullable
            placeholder="Never retire"
            hint="Salary stops after this year. Empty keeps working through the forecast."
            onChange={(v) => onChange({ retirementYear: v })}
          />
          <MoneyField
            label="Retirement income per year"
            value={state.retirementIncomeMinor}
            onChange={(v) => onChange({ retirementIncomeMinor: v ?? 0 })}
            dataText={`In ${base} dollars (the starting year), before tax; grows with inflation.`}
          />
        </section>

        <EventsEditor events={state.events} baseYear={base} onChange={(events) => onChange({ events })} />
      </CardContent>
    </Card>
  );
}

/** " Shown as $X in 2025 at 3%/yr income growth." when the data's year is not the starting year. */
function rebasedNote(
  dataYear: number | null | undefined,
  base: number,
  rebasedMinor: number | null,
  growthPct: number,
  growthName: string,
): string {
  if (!dataYear || dataYear === base || rebasedMinor === null) return "";
  return `. Rebased to ${base}: ${formatUsd(rebasedMinor)} (${dataYear} to ${base} at ${formatPct(growthPct)}/yr ${growthName}).`;
}

function EventsEditor({
  events,
  baseYear,
  onChange,
}: {
  events: ForecastState["events"];
  baseYear: number;
  onChange: (events: ForecastState["events"]) => void;
}) {
  const update = (i: number, patch: Partial<ForecastState["events"][number]>) =>
    onChange(events.map((e, j) => (j === i ? { ...e, ...patch } : e)));
  return (
    <section aria-label="One-off events" className={SECTION}>
      <h3 className={SECTION_TITLE}>One-off events</h3>
      <p className="text-xs text-muted-foreground">
        A lump sum in a given year, in that year&apos;s dollars: negative for money out (a down payment), positive for
        money in (an inheritance).
      </p>
      {events.length === 0 ? <p className="text-sm text-muted-foreground">No events.</p> : null}
      <ul className="flex flex-col gap-3">
        {events.map((event, i) => (
          <li key={i} className="grid grid-cols-2 items-end gap-2 rounded-md border border-border p-3">
            <YearField
              label={`Event ${i + 1} year`}
              value={event.year}
              min={MIN_YEAR}
              max={MAX_YEAR}
              onChange={(v) => v !== null && update(i, { year: v })}
            />
            <MoneyField
              label={`Event ${i + 1} amount`}
              value={event.amountMinor}
              allowNegative
              onChange={(v) => update(i, { amountMinor: v ?? 0 })}
            />
            <EventLabel index={i} value={event.label} onChange={(label) => update(i, { label })} />
            {event.year <= baseYear ? (
              <p role="alert" className="col-span-2 text-xs text-destructive">
                Event {i + 1} is ignored: the projection starts after {baseYear}, so pick a later year.
              </p>
            ) : null}
            <Button
              type="button"
              variant="outline"
              size="sm"
              aria-label={`Remove event ${i + 1}`}
              onClick={() => onChange(events.filter((_, j) => j !== i))}
            >
              Remove
            </Button>
          </li>
        ))}
      </ul>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="self-start"
        disabled={events.length >= MAX_EVENTS}
        onClick={() =>
          onChange([...events, { year: (events[events.length - 1]?.year ?? new Date().getFullYear()) + 1, amountMinor: 0, label: "" }])
        }
      >
        Add event
      </Button>
    </section>
  );
}

function EventLabel({ index, value, onChange }: { index: number; value: string; onChange: (v: string) => void }) {
  const id = React.useId();
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs font-medium text-muted-foreground">
        Event {index + 1} label
      </label>
      <Input id={id} maxLength={100} value={value} placeholder="e.g. House down payment" onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}
