import * as React from "react";

import type { components } from "@/api/client";
import { Money } from "@/components/primitives/money";
import { EmptyState } from "@/components/primitives/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/use-toast";

import { useDeleteBalanceEntry, usePatchBalanceEntry } from "./api";
import { categoryLabel, entryPeriod, formatBalanceInput, parseBalanceInput, periodLabel } from "./logic";

type Entry = components["schemas"]["BalanceEntry"];
type User = components["schemas"]["User"];

const PAGE_LIMIT = 50;

export interface EntriesTableProps {
  entries: readonly Entry[];
  users: readonly User[];
  /** Show an owner column (All users view). */
  showUser: boolean;
}

/** Every entered balance, newest period first, filterable by period. Balance and note are
 * editable in place; identity (account/period/category) is not (re-enter to correct those). */
export function EntriesTable({ entries, users, showUser }: EntriesTableProps) {
  const { toast } = useToast();
  const patch = usePatchBalanceEntry();
  const remove = useDeleteBalanceEntry();
  // Unique per instance: compare mode renders two copies of this screen.
  const uid = React.useId();
  const [period, setPeriod] = React.useState("");
  const [showAll, setShowAll] = React.useState(false);
  const [editingId, setEditingId] = React.useState<number | null>(null);
  const [balance, setBalance] = React.useState("");
  const [note, setNote] = React.useState("");
  const [editError, setEditError] = React.useState<string | null>(null);
  const [confirmId, setConfirmId] = React.useState<number | null>(null);

  const periods = React.useMemo(
    () => Array.from(new Set(entries.map(entryPeriod))).sort().reverse(),
    [entries],
  );
  const sorted = React.useMemo(
    () =>
      entries
        .filter((e) => !period || entryPeriod(e) === period)
        .sort(
          (a, b) =>
            entryPeriod(b).localeCompare(entryPeriod(a)) ||
            a.category.localeCompare(b.category) ||
            a.account_name.localeCompare(b.account_name),
        ),
    [entries, period],
  );
  const visible = showAll ? sorted : sorted.slice(0, PAGE_LIMIT);
  const userName = (id: number) => users.find((u) => u.id === id)?.name ?? `User ${id}`;

  function startEdit(entry: Entry) {
    setEditingId(entry.id);
    setBalance(formatBalanceInput(entry.balance_minor));
    setNote(entry.note ?? "");
    setEditError(null);
    setConfirmId(null);
  }

  function saveEdit(entry: Entry) {
    const minor = parseBalanceInput(balance);
    if (minor === null) {
      setEditError("Enter a non-negative amount.");
      return;
    }
    patch.mutate(
      { id: entry.id, balance_minor: minor, note: note.trim() ? note.trim() : null },
      {
        onSuccess: () => {
          setEditingId(null);
          toast({ variant: "success", title: "Updated" });
        },
        onError: (error) =>
          toast({
            variant: "destructive",
            title: "Could not update entry",
            description: error instanceof Error ? error.message : String(error),
          }),
      },
    );
  }

  function confirmDelete(entry: Entry) {
    remove.mutate(entry.id, {
      onSuccess: () => {
        setConfirmId(null);
        toast({ title: "Deleted", description: `${entry.account_name} · ${periodLabel(entryPeriod(entry))}` });
      },
      onError: (error) =>
        toast({
          variant: "destructive",
          title: "Could not delete entry",
          description: error instanceof Error ? error.message : String(error),
        }),
    });
  }

  if (entries.length === 0) {
    return <EmptyState title="No entries yet" description="Balances you save will be listed here." />;
  }

  return (
    <div className="flex flex-col gap-3" data-testid="entries-table">
      <div className="flex items-center gap-2">
        <label htmlFor={`${uid}-nw-entries-period`} className="text-xs font-medium text-muted-foreground">
          Filter by period
        </label>
        <select
          id={`${uid}-nw-entries-period`}
          className="h-8 rounded-md border border-input bg-transparent px-2 text-sm"
          value={period}
          onChange={(e) => {
            setPeriod(e.target.value);
            setShowAll(false);
          }}
        >
          <option value="">All periods</option>
          {periods.map((p) => (
            <option key={p} value={p}>
              {periodLabel(p)}
            </option>
          ))}
        </select>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <caption className="sr-only">Balance entries</caption>
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th scope="col" className="py-2 pr-3 font-medium">Period</th>
              {showUser ? <th scope="col" className="py-2 pr-3 font-medium">User</th> : null}
              <th scope="col" className="py-2 pr-3 font-medium">Category</th>
              <th scope="col" className="py-2 pr-3 font-medium">Account</th>
              <th scope="col" className="py-2 pr-3 text-right font-medium">Balance</th>
              <th scope="col" className="py-2 pr-3 font-medium">Note</th>
              <th scope="col" className="py-2 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((entry) => {
              const editing = editingId === entry.id;
              const label = `${entry.account_name} ${periodLabel(entryPeriod(entry))}`;
              return (
                <tr key={entry.id} className="border-b border-border/60 align-middle">
                  <td className="py-2 pr-3 whitespace-nowrap">{periodLabel(entryPeriod(entry))}</td>
                  {showUser ? <td className="py-2 pr-3">{userName(entry.user_id)}</td> : null}
                  <td className="py-2 pr-3">{categoryLabel(entry.category)}</td>
                  <td className="py-2 pr-3">{entry.account_name}</td>
                  <td className="py-2 pr-3 text-right">
                    {editing ? (
                      <Input
                        aria-label={`Balance for ${label}`}
                        inputMode="decimal"
                        className="ml-auto h-8 w-32 text-right"
                        value={balance}
                        onChange={(e) => setBalance(e.target.value)}
                        aria-invalid={editError ? true : undefined}
                      />
                    ) : (
                      <Money
                        tone="neutral"
                        minorUnits={entry.balance_minor}
                        currency={entry.currency}
                      />
                    )}
                    {editing && editError ? (
                      <p role="alert" className="text-xs text-destructive">{editError}</p>
                    ) : null}
                  </td>
                  <td className="py-2 pr-3 text-muted-foreground">
                    {editing ? (
                      <Input
                        aria-label={`Note for ${label}`}
                        className="h-8"
                        value={note}
                        onChange={(e) => setNote(e.target.value)}
                      />
                    ) : (
                      (entry.note ?? "")
                    )}
                  </td>
                  <td className="py-2 text-right whitespace-nowrap">
                    {editing ? (
                      <span className="inline-flex gap-1">
                        <Button size="sm" onClick={() => saveEdit(entry)} disabled={patch.isPending}>
                          Save
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setEditingId(null)}>
                          Cancel
                        </Button>
                      </span>
                    ) : confirmId === entry.id ? (
                      <span className="inline-flex gap-1">
                        <Button
                          size="sm"
                          variant="destructive"
                          aria-label={`Confirm delete ${label}`}
                          onClick={() => confirmDelete(entry)}
                          disabled={remove.isPending}
                        >
                          Confirm?
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => setConfirmId(null)}>
                          Cancel
                        </Button>
                      </span>
                    ) : (
                      <span className="inline-flex gap-1">
                        <Button
                          size="sm"
                          variant="ghost"
                          aria-label={`Edit ${label}`}
                          onClick={() => startEdit(entry)}
                        >
                          Edit
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          aria-label={`Delete ${label}`}
                          onClick={() => {
                            setConfirmId(entry.id);
                            setEditingId(null);
                          }}
                        >
                          Delete
                        </Button>
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
            {visible.length === 0 ? (
              <tr>
                <td colSpan={showUser ? 7 : 6} className="py-6 text-center text-muted-foreground">
                  No entries for this period.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      {!showAll && sorted.length > PAGE_LIMIT ? (
        <Button variant="outline" size="sm" className="self-start" onClick={() => setShowAll(true)}>
          Show all {sorted.length} entries
        </Button>
      ) : null}
    </div>
  );
}
