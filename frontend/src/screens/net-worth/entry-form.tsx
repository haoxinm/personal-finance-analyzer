import * as React from "react";

import type { components } from "@/api/client";
import { UserSwitcher } from "@/components/primitives/user-switcher";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/use-toast";

import { useUpsertBalanceEntry, type BalanceEntryUpsert } from "./api";
import {
  CATEGORIES,
  CATEGORY_LABELS,
  MONTH_LABELS,
  accountSuggestions,
  parseBalanceInput,
  periodLabel,
  toPeriod,
  type Category,
} from "./logic";

type User = components["schemas"]["User"];
type Entry = components["schemas"]["BalanceEntry"];

export interface BalanceEntryFormProps {
  users: readonly User[];
  /** The selected user, or null for "All users" (the form then asks who). */
  selectedUserId: number | null;
  /** Existing entries, used for account-name suggestions. */
  entries: readonly Entry[];
}

/** The API only accepts USD balances (`BalanceEntryUpsert.currency` is `Literal["USD"]`), so the
 * form always saves in it, whatever display currency is selected. */
const ENTRY_CURRENCY: BalanceEntryUpsert["currency"] = "USD";

const LABEL = "text-xs font-medium text-muted-foreground";
const SELECT =
  "flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

/** Add or overwrite one account balance for a month. Saving the same user/category/account/
 * month again updates that entry (the API upserts), so re-typing a month is a correction. */
export function BalanceEntryForm({ users, selectedUserId, entries }: BalanceEntryFormProps) {
  const { toast } = useToast();
  const upsert = useUpsertBalanceEntry();
  const now = React.useMemo(() => new Date(), []);

  // Unique per instance: compare mode renders two copies of this screen.
  const uid = React.useId();
  const [chosenUserId, setChosenUserId] = React.useState<number | null>(null);
  const [category, setCategory] = React.useState<Category>("checking");
  const [account, setAccount] = React.useState("");
  const [year, setYear] = React.useState(String(now.getFullYear()));
  const [month, setMonth] = React.useState(now.getMonth() + 1);
  const [balance, setBalance] = React.useState("");
  const [note, setNote] = React.useState("");
  const [errors, setErrors] = React.useState<Record<string, string>>({});

  const defaultUser = users.find((u) => u.is_default) ?? users[0];
  const askUser = selectedUserId === null && users.length > 1;
  const userId =
    selectedUserId ??
    (chosenUserId !== null && users.some((u) => u.id === chosenUserId) ? chosenUserId : null) ??
    defaultUser?.id ??
    null;
  const userName = users.find((u) => u.id === userId)?.name;

  const suggestions = React.useMemo(() => {
    const scoped = userId === null ? entries : entries.filter((e) => e.user_id === userId);
    return accountSuggestions(scoped, category);
  }, [entries, userId, category]);

  function validate() {
    const next: Record<string, string> = {};
    if (userId === null) next["user"] = "Add a user in Settings first.";
    if (!account.trim()) next["account"] = "Enter an account name.";
    else if (account.trim().length > 100) next["account"] = "Account name must be 100 characters or fewer.";
    const y = Number(year);
    if (!Number.isInteger(y) || y < 1900 || y > 2200) next["year"] = "Enter a year between 1900 and 2200.";
    const minor = parseBalanceInput(balance);
    if (minor === null) next["balance"] = "Enter a non-negative amount, like 1,234.56.";
    if (note.length > 500) next["note"] = "Note must be 500 characters or fewer.";
    return { next, minor, y };
  }

  function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    const { next, minor, y } = validate();
    setErrors(next);
    if (Object.keys(next).length > 0 || minor === null || userId === null) return;
    upsert.mutate(
      {
        user_id: userId,
        category,
        account_name: account.trim(),
        year: y,
        month,
        balance_minor: minor,
        currency: ENTRY_CURRENCY,
        note: note.trim() ? note.trim() : null,
      },
      {
        onSuccess: ({ created }) => {
          toast({
            variant: "success",
            title: created ? "Saved" : "Updated existing entry",
            description: `${account.trim()} · ${periodLabel(toPeriod(y, month))}`,
          });
          // Keep category/account/period for quick multi-account entry; clear the amount.
          setBalance("");
          setNote("");
        },
        onError: (error) => {
          toast({
            variant: "destructive",
            title: "Could not save balance",
            description: error instanceof Error ? error.message : String(error),
          });
        },
      },
    );
  }

  const errorId = (name: string) => `${uid}-nw-form-${name}-error`;
  const field = (name: string) =>
    errors[name]
      ? { "aria-invalid": true as const, "aria-describedby": errorId(name) }
      : {};
  const fieldError = (name: string) =>
    errors[name] ? (
      <p id={errorId(name)} role="alert" className="text-xs text-destructive">
        {errors[name]}
      </p>
    ) : null;

  return (
    <form
      onSubmit={onSubmit}
      noValidate
      aria-label="Add balance"
      className="grid gap-3 sm:grid-cols-2 lg:grid-cols-6"
    >
      {askUser ? (
        <div className="flex flex-col gap-1 sm:col-span-2 lg:col-span-6">
          <span className={LABEL}>Who is this for?</span>
          <UserSwitcher
            users={users}
            value={userId}
            onChange={(id) => setChosenUserId(id)}
            allowAll={false}
            variant="select"
            aria-label="Account owner"
            className="w-full sm:w-56"
          />
        </div>
      ) : null}
      {errors["user"] ? <p role="alert" className="text-xs text-destructive sm:col-span-2 lg:col-span-6">{errors["user"]}</p> : null}

      <div className="flex flex-col gap-1 lg:col-span-1">
        <label htmlFor={`${uid}-nw-category`} className={LABEL}>
          Category
        </label>
        <select
          id={`${uid}-nw-category`}
          className={SELECT}
          value={category}
          onChange={(e) => setCategory(e.target.value as Category)}
        >
          {CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {CATEGORY_LABELS[c]}
            </option>
          ))}
        </select>
      </div>

      <div className="flex flex-col gap-1 lg:col-span-2">
        <label htmlFor={`${uid}-nw-account`} className={LABEL}>
          Account name
        </label>
        <Input
          id={`${uid}-nw-account`}
          list={`${uid}-nw-account-suggestions`}
          autoComplete="off"
          placeholder="e.g. Chase checking"
          value={account}
          onChange={(e) => setAccount(e.target.value)}
          {...field("account")}
        />
        <datalist id={`${uid}-nw-account-suggestions`} data-testid="account-suggestions">
          {suggestions.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>
        {fieldError("account")}
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor={`${uid}-nw-month`} className={LABEL}>
          Month
        </label>
        <select
          id={`${uid}-nw-month`}
          className={SELECT}
          value={month}
          onChange={(e) => setMonth(Number(e.target.value))}
        >
          {MONTH_LABELS.map((label, i) => (
            <option key={label} value={i + 1}>
              {label}
            </option>
          ))}
        </select>
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor={`${uid}-nw-year`} className={LABEL}>
          Year
        </label>
        <Input
          id={`${uid}-nw-year`}
          type="number"
          inputMode="numeric"
          min={1900}
          max={2200}
          value={year}
          onChange={(e) => setYear(e.target.value)}
          {...field("year")}
        />
        {fieldError("year")}
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor={`${uid}-nw-balance`} className={LABEL}>
          {category === "credit_card" ? "Amount owed" : "Balance"} ({ENTRY_CURRENCY})
        </label>
        <Input
          id={`${uid}-nw-balance`}
          inputMode="decimal"
          autoComplete="off"
          placeholder="0.00"
          value={balance}
          onChange={(e) => setBalance(e.target.value)}
          {...field("balance")}
        />
        {category === "credit_card" ? (
          <p className="text-xs text-muted-foreground">Enter what you owe as a positive number.</p>
        ) : null}
        {fieldError("balance")}
      </div>

      <div className="flex flex-col gap-1 sm:col-span-2 lg:col-span-5">
        <label htmlFor={`${uid}-nw-note`} className={LABEL}>
          Note (optional)
        </label>
        <Input
          id={`${uid}-nw-note`}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          {...field("note")}
        />
        {fieldError("note")}
      </div>

      <div className="flex items-end">
        <Button type="submit" className="w-full" disabled={upsert.isPending}>
          {upsert.isPending ? "Saving…" : "Save balance"}
        </Button>
      </div>
      {userName && users.length > 1 ? (
        <p className="text-xs text-muted-foreground sm:col-span-2 lg:col-span-6">
          Saving for {userName}. Saving the same account and month again replaces its balance.
        </p>
      ) : (
        <p className="text-xs text-muted-foreground sm:col-span-2 lg:col-span-6">
          Saving the same account and month again replaces its balance.
        </p>
      )}
    </form>
  );
}
