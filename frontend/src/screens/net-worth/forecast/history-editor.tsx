import * as React from "react";

import { Money } from "@/components/primitives/money";
import { EmptyState } from "@/components/primitives/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/use-toast";

import { useDeleteHistory, usePutHistory } from "./api";
import { MoneyField } from "./fields";
import { MIN_YEAR, formatUsd, type HistoryRow } from "./params";

export interface HistoryEditorProps {
  /** The person whose past years are edited; null = everyone combined (read-only). */
  userId: number | null;
  history: readonly HistoryRow[];
}

const SOURCE_LABEL: Record<string, string> = {
  entries: "Balance entries",
  manual: "Manual",
  mixed: "Mixed (per person)",
};

/** Year-end net worth per year: what the balance entries give, with a manual value for years
 * they don't cover (or that predate them). Entries take precedence over a manual value. The same
 * table shows the income, tax and spending that feed the suggested defaults. */
export function HistoryEditor({ userId, history }: HistoryEditorProps) {
  const rows = [...history].sort((a, b) => b.year - a.year);
  const readOnly = userId === null;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">History</CardTitle>
        <p className="text-sm text-muted-foreground">
          Year-end net worth comes from your balance entries. For a year they don&apos;t cover, enter it by hand;
          a manual value is ignored for any year that has entries.
        </p>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {readOnly ? (
          <p className="text-sm text-muted-foreground" role="note">
            Showing everyone combined. Pick a person in the user bar to add or change a manual year.
          </p>
        ) : null}
        {rows.length === 0 ? (
          <EmptyState
            title="No history yet"
            description="Add balances on the Overview tab or tax forms under Income & Tax, or enter a past year's net worth below."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <caption className="sr-only">Net worth, income, tax and spending by year</caption>
              <thead>
                <tr className="border-b border-border text-left text-xs text-muted-foreground">
                  <th scope="col" className="py-2 pr-3 font-medium">Year</th>
                  <th scope="col" className="py-2 pr-3 text-right font-medium">Net worth</th>
                  <th scope="col" className="py-2 pr-3 font-medium">Source</th>
                  <th scope="col" className="py-2 pr-3 text-right font-medium">Income</th>
                  <th scope="col" className="py-2 pr-3 text-right font-medium">Tax</th>
                  <th scope="col" className="py-2 pr-3 text-right font-medium">Spending</th>
                  <th scope="col" className="py-2 pr-3 text-right font-medium">Savings</th>
                  <th scope="col" className="py-2 font-medium">Manual net worth</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <HistoryTableRow key={row.year} row={row} userId={userId} />
                ))}
              </tbody>
            </table>
          </div>
        )}
        {readOnly ? null : <AddYearForm userId={userId} />}
      </CardContent>
    </Card>
  );
}

const dash = <span aria-label="no data">—</span>;
const usd = (minor: number | null | undefined) => (minor == null ? dash : formatUsd(minor));

function HistoryTableRow({ row, userId }: { row: HistoryRow; userId: number | null }) {
  const hasEntries = row.source === "entries";
  return (
    <tr className="border-b border-border align-top last:border-b-0">
      <th scope="row" className="py-2 pr-3 text-left font-medium">
        {row.year}
      </th>
      <td className="py-2 pr-3 text-right">
        {row.net_worth_minor == null ? dash : <Money tone="neutral" minorUnits={row.net_worth_minor} />}
      </td>
      <td className="py-2 pr-3">
        {row.source ? (
          <Badge variant={hasEntries ? "secondary" : "outline"} title={row.as_of ? `As of ${row.as_of}` : undefined}>
            {SOURCE_LABEL[row.source] ?? row.source}
          </Badge>
        ) : (
          dash
        )}
      </td>
      <td className="py-2 pr-3 text-right tabular-nums">
        {usd(row.income_minor)}
        {row.earned_income_minor != null && row.earned_income_minor !== row.income_minor ? (
          <span className="block text-xs text-muted-foreground">{formatUsd(row.earned_income_minor)} earned</span>
        ) : null}
      </td>
      <td className="py-2 pr-3 text-right tabular-nums">{usd(row.tax_minor)}</td>
      <td className="py-2 pr-3 text-right tabular-nums">
        {usd(row.spending_minor)}
        {row.spending_minor != null && row.spending_months < 12 ? (
          <span className="block text-xs text-muted-foreground">{row.spending_months} mo</span>
        ) : null}
      </td>
      <td className="py-2 pr-3 text-right tabular-nums">{usd(row.savings_minor)}</td>
      <td className="py-2">
        {hasEntries ? (
          <p className="text-xs text-muted-foreground">
            Balance entries take precedence
            {row.manual_net_worth_minor != null ? ` (manual ${formatUsd(row.manual_net_worth_minor)} ignored)` : ""}.
          </p>
        ) : userId === null ? (
          dash
        ) : (
          <ManualCell year={row.year} userId={userId} manual={row.manual_net_worth_minor} note={row.manual_note} />
        )}
      </td>
    </tr>
  );
}

function ManualCell({ year, userId, manual, note }: { year: number; userId: number; manual: number | null; note: string | null }) {
  const { toast } = useToast();
  const put = usePutHistory();
  const del = useDeleteHistory();
  const [value, setValue] = React.useState<number | null>(manual);
  React.useEffect(() => setValue(manual), [manual]);
  const dirty = value !== null && value !== manual;

  const onError = (title: string) => (err: unknown) =>
    toast({ variant: "destructive", title, description: err instanceof Error ? err.message : String(err) });

  return (
    <div className="flex flex-wrap items-start gap-2">
      <MoneyField
        label={`Manual net worth for ${year}`}
        hideLabel
        nullable
        allowNegative
        value={value}
        onChange={setValue}
        className="w-40"
      />
      <Button
        type="button"
        size="sm"
        variant="outline"
        disabled={!dirty || put.isPending}
        aria-label={`Save ${year}`}
        onClick={() =>
          value !== null &&
          put.mutate({ userId, year, amountMinor: value, note }, { onError: onError("Could not save net worth") })
        }
      >
        Save
      </Button>
      {manual !== null ? (
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={del.isPending}
          aria-label={`Clear ${year}`}
          onClick={() => del.mutate({ userId, year }, { onError: onError("Could not clear net worth") })}
        >
          Clear
        </Button>
      ) : null}
    </div>
  );
}

/** Adds a net worth for a year the table doesn't list yet (typically before any data existed). */
function AddYearForm({ userId }: { userId: number }) {
  const uid = React.useId();
  const { toast } = useToast();
  const put = usePutHistory();
  const thisYear = new Date().getFullYear();
  const [year, setYear] = React.useState(String(thisYear - 1));
  const [amount, setAmount] = React.useState<number | null>(null);
  const [note, setNote] = React.useState("");
  const [errors, setErrors] = React.useState<{ year?: string; amount?: string }>({});

  function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    const y = Number(year);
    const next: typeof errors = {};
    if (!Number.isInteger(y) || y < MIN_YEAR || y > thisYear) next.year = `Enter a year from ${MIN_YEAR} to ${thisYear}.`;
    if (amount === null) next.amount = "Enter the net worth at the end of that year (negative if you owed more than you owned).";
    setErrors(next);
    if (Object.keys(next).length > 0 || amount === null) return;
    put.mutate(
      { userId, year: y, amountMinor: amount, note: note.trim() ? note.trim() : null },
      {
        onSuccess: () => {
          toast({ variant: "success", title: "Saved", description: `Net worth for ${y}` });
          setAmount(null);
          setNote("");
        },
        onError: (err) =>
          toast({ variant: "destructive", title: "Could not save net worth", description: err instanceof Error ? err.message : String(err) }),
      },
    );
  }

  return (
    <form onSubmit={onSubmit} noValidate aria-label="Add a past year" className="grid gap-3 border-t border-border pt-4 sm:grid-cols-4">
      <div className="flex flex-col gap-1">
        <label htmlFor={`${uid}-year`} className="text-xs font-medium text-muted-foreground">
          Year
        </label>
        <Input
          id={`${uid}-year`}
          type="number"
          inputMode="numeric"
          min={MIN_YEAR}
          max={thisYear}
          value={year}
          onChange={(e) => setYear(e.target.value)}
          aria-invalid={errors.year ? true : undefined}
          aria-describedby={errors.year ? `${uid}-year-error` : undefined}
        />
        {errors.year ? (
          <p id={`${uid}-year-error`} role="alert" className="text-xs text-destructive">
            {errors.year}
          </p>
        ) : null}
      </div>
      <div className="flex flex-col gap-1">
        <MoneyField label="Year-end net worth" value={amount} nullable allowNegative onChange={setAmount} />
        {errors.amount ? (
          <p role="alert" className="text-xs text-destructive">
            {errors.amount}
          </p>
        ) : null}
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${uid}-note`} className="text-xs font-medium text-muted-foreground">
          Note (optional)
        </label>
        <Input id={`${uid}-note`} maxLength={500} value={note} onChange={(e) => setNote(e.target.value)} />
      </div>
      <div className="flex items-end">
        <Button type="submit" className="w-full" disabled={put.isPending}>
          {put.isPending ? "Saving…" : "Add past year"}
        </Button>
      </div>
    </form>
  );
}
