import * as React from "react";
import { Link } from "react-router-dom";

import { useStatements } from "@/api/hooks";
import { SegmentedControl } from "@/components/primitives/segmented-control";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useToast } from "@/components/ui/use-toast";

import { parseAmountInput } from "./amount-input";
import { useAccountOptions, useCreateTransaction } from "./api";
import { isoDateFromToday, outsideStatementPeriod } from "./dates";
import { KINDS, type Category, type Kind, type Transaction } from "./types";

type Direction = "out" | "in";

const DIRECTION_OPTIONS = [
  { value: "out", label: "Money out" },
  { value: "in", label: "Money in" },
] as const;

const MIN_DATE = "1970-01-01";

// eslint-disable-next-line no-control-regex -- rejecting control characters is the point
const CONTROL_CHARS = /[\u0000-\u001f\u007f]/;

/** The first problem with a typed date, or null. The browser's `min`/`max` do not stop typing. */
function dateProblem(date: string): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || Number.isNaN(Date.parse(`${date}T00:00:00Z`))) {
    return "Enter a valid date.";
  }
  if (date < MIN_DATE || date > isoDateFromToday(366)) {
    return "Enter a date between 1970 and a year from now.";
  }
  return null;
}

export interface AddTransactionDialogProps {
  /** Preselects this statement (from the statement filter or the Import page's link). */
  initialStatementId?: number;
  /** The selected user view: only that person's statements are offered. */
  userId?: number;
  categories: Category[];
  onClose: () => void;
}

/**
 * "Add transaction": files a missed line under an imported statement by hand (`POST
 * /transactions`). The account and person come from the statement. Kind and category default to
 * "Automatic": the server then decides from the description and the rules, never with the LLM, and
 * a line nothing recognises lands in Review. A server refusal is shown inline.
 */
export function AddTransactionDialog({
  initialStatementId,
  userId,
  categories,
  onClose,
}: AddTransactionDialogProps) {
  const ids = React.useId();
  const { toast } = useToast();
  const create = useCreateTransaction();
  const statementsQuery = useStatements(userId);
  const accounts = useAccountOptions();

  const statements = React.useMemo(
    () => (statementsQuery.data ?? []).filter((statement) => statement.status === "parsed"),
    [statementsQuery.data],
  );

  const [statementChoice, setStatementChoice] = React.useState<string | null>(null);
  // The preset wins until the user picks; a lone statement is chosen for them.
  const statementId =
    statementChoice ??
    (initialStatementId !== undefined && statements.some((s) => s.id === initialStatementId)
      ? String(initialStatementId)
      : statements.length === 1
        ? String(statements[0]!.id)
        : "");
  const statement = statements.find((s) => String(s.id) === statementId);

  const [postedDate, setPostedDate] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [direction, setDirection] = React.useState<Direction>("out");
  const [amountText, setAmountText] = React.useState("");
  const [kind, setKind] = React.useState<Kind | "">("");
  const [categoryKey, setCategoryKey] = React.useState("");
  const [subcategoryKey, setSubcategoryKey] = React.useState("");
  const [notes, setNotes] = React.useState("");
  const [submitError, setSubmitError] = React.useState<string | null>(null);
  // Errors show once the user has tried to save (or left a field), not while they are still typing.
  const [touched, setTouched] = React.useState(false);

  const selectedCategory = categories.find((c) => c.key === categoryKey);

  const trimmedDescription = description.trim();
  const statementError = statement === undefined ? "Choose the statement this belongs to." : null;
  const dateError = dateProblem(postedDate);
  const descriptionError =
    trimmedDescription === ""
      ? "Enter a description."
      : trimmedDescription.length > 200
        ? "Use at most 200 characters."
        : CONTROL_CHARS.test(trimmedDescription)
          ? "Remove the control characters from the description."
          : null;
  const parsedAmount = parseAmountInput(amountText);
  const amountError =
    parsedAmount === null
      ? "Enter an amount above 0, with at most 2 decimals (for example 12.50)."
      : null;
  const categoryError =
    categoryKey !== "" && subcategoryKey === "" ? "Choose a subcategory too, or set the category back to Automatic." : null;
  const signedAmount = parsedAmount === null ? null : direction === "out" ? parsedAmount : -parsedAmount;
  const valid =
    statementError === null &&
    dateError === null &&
    descriptionError === null &&
    amountError === null &&
    categoryError === null;

  const periodWarning =
    dateError === null && statement !== undefined
      ? outsideStatementPeriod(postedDate, statement.period_start, statement.period_end)
      : false;

  const currency = statement?.currency ?? "USD";
  const accountLabel = (accountId: number | null) =>
    accountId === null ? undefined : accounts.data?.find((a) => a.id === accountId)?.label;

  function statementLabel(s: (typeof statements)[number]): string {
    const name = s.original_name || `Statement #${s.id}`;
    const period = s.period_start && s.period_end ? `${s.period_start} to ${s.period_end}` : "no period";
    return [name, accountLabel(s.account_id), period].filter(Boolean).join(" · ");
  }

  async function submit() {
    setTouched(true);
    setSubmitError(null);
    if (!valid || statement === undefined || signedAmount === null) return;
    try {
      const created: Transaction = await create.mutateAsync({
        statement_id: statement.id,
        posted_date: postedDate,
        description: trimmedDescription,
        amount_minor: signedAmount,
        ...(kind !== "" ? { kind } : {}),
        ...(categoryKey !== "" ? { category_key: categoryKey, subcategory_key: subcategoryKey } : {}),
        ...(notes.trim() !== "" ? { notes: notes.trim() } : {}),
      });
      toast({
        title: "Transaction added",
        description:
          created.needs_review && created.category_key === null
            ? "No rule recognised it, so it is waiting in Review."
            : undefined,
      });
      onClose();
    } catch (error) {
      setSubmitError(
        error instanceof Error && error.message ? error.message : "Could not add this transaction.",
      );
    }
  }

  const pending = create.isPending;
  const fieldClass =
    "h-9 rounded-md border border-input bg-transparent px-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring aria-[invalid=true]:border-destructive";
  const show = (message: string | null) => (touched ? message : null);

  return (
    <Dialog
      onClose={onClose}
      onRequestClose={() => {
        if (!pending) onClose();
      }}
      aria-labelledby={`${ids}-title`}
    >
      <DialogHeader>
        <DialogTitle id={`${ids}-title`}>Add transaction</DialogTitle>
        <DialogDescription>
          Add a line the statement parser missed. It is filed under the statement you choose and counts
          in totals like any other row.
        </DialogDescription>
      </DialogHeader>
      <DialogBody>
        {statementsQuery.isLoading ? (
          <p className="text-sm text-muted-foreground">Loading statements…</p>
        ) : statementsQuery.isError ? (
          <p role="alert" className="text-sm text-destructive">
            Could not load your statements
            {statementsQuery.error instanceof Error ? `: ${statementsQuery.error.message}` : "."}
          </p>
        ) : statements.length === 0 ? (
          <p className="text-sm text-muted-foreground" data-testid="no-statements">
            There is no imported statement to add this to yet.{" "}
            <Link className="text-primary underline underline-offset-2" to="/spending/import">
              Import a statement
            </Link>{" "}
            first.
          </p>
        ) : null}

        <div className="flex flex-col gap-1">
          <label htmlFor={`${ids}-statement`} className="text-xs text-muted-foreground">
            Statement
          </label>
          <select
            id={`${ids}-statement`}
            className={fieldClass}
            value={statementId}
            aria-invalid={show(statementError) !== null}
            disabled={statements.length === 0}
            onChange={(e) => setStatementChoice(e.target.value)}
          >
            <option value="">Choose a statement</option>
            {statements.map((s) => (
              <option key={s.id} value={s.id}>
                {statementLabel(s)}
              </option>
            ))}
          </select>
          {show(statementError) ? <FieldError>{statementError}</FieldError> : null}
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor={`${ids}-date`} className="text-xs text-muted-foreground">
            Date
          </label>
          <input
            id={`${ids}-date`}
            type="date"
            className={fieldClass}
            value={postedDate}
            min={MIN_DATE}
            max={isoDateFromToday(366)}
            aria-invalid={show(dateError) !== null}
            onChange={(e) => setPostedDate(e.target.value)}
          />
          {show(dateError) ? <FieldError>{dateError}</FieldError> : null}
          {periodWarning ? (
            <p
              role="status"
              data-testid="period-warning"
              className="rounded-md border border-amber-300 bg-amber-50 px-2 py-1 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200"
            >
              This date is outside the statement’s period ({statement?.period_start} to{" "}
              {statement?.period_end}). You can still add it.
            </p>
          ) : null}
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor={`${ids}-desc`} className="text-xs text-muted-foreground">
            Description
          </label>
          <input
            id={`${ids}-desc`}
            type="text"
            maxLength={200}
            autoComplete="off"
            className={fieldClass}
            value={description}
            aria-invalid={show(descriptionError) !== null}
            onChange={(e) => setDescription(e.target.value)}
          />
          {show(descriptionError) ? <FieldError>{descriptionError}</FieldError> : null}
        </div>

        <div className="flex flex-col gap-1">
          <span id={`${ids}-amount-label`} className="text-xs text-muted-foreground">
            Amount ({currency})
          </span>
          <div className="flex flex-wrap items-center gap-2">
            <SegmentedControl
              label="Direction"
              size="sm"
              value={direction}
              options={DIRECTION_OPTIONS}
              onChange={setDirection}
            />
            <input
              type="text"
              inputMode="decimal"
              autoComplete="off"
              aria-labelledby={`${ids}-amount-label`}
              placeholder="0.00"
              className={`${fieldClass} w-32 min-w-0 flex-1 text-right tabular-nums`}
              value={amountText}
              aria-invalid={show(amountError) !== null}
              onChange={(e) => setAmountText(e.target.value)}
            />
          </div>
          {show(amountError) ? <FieldError>{amountError}</FieldError> : null}
          <p className="text-xs text-muted-foreground">
            {direction === "out"
              ? "Money out: a purchase, fee or payment you made."
              : "Money in: a refund, deposit or credit."}
          </p>
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor={`${ids}-kind`} className="text-xs text-muted-foreground">
            Kind
          </label>
          <select
            id={`${ids}-kind`}
            className={fieldClass}
            value={kind}
            onChange={(e) => setKind(e.target.value as Kind | "")}
          >
            <option value="">Automatic</option>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor={`${ids}-category`} className="text-xs text-muted-foreground">
            Category
          </label>
          <select
            id={`${ids}-category`}
            className={fieldClass}
            value={categoryKey}
            onChange={(e) => {
              setCategoryKey(e.target.value);
              setSubcategoryKey("");
            }}
          >
            <option value="">Automatic</option>
            {categories.map((category) => (
              <option key={category.key} value={category.key}>
                {category.name}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor={`${ids}-subcategory`} className="text-xs text-muted-foreground">
            Subcategory
          </label>
          <select
            id={`${ids}-subcategory`}
            className={fieldClass}
            value={subcategoryKey}
            disabled={!selectedCategory}
            aria-invalid={show(categoryError) !== null}
            onChange={(e) => setSubcategoryKey(e.target.value)}
          >
            <option value="">{selectedCategory ? "Choose a subcategory" : "Automatic"}</option>
            {selectedCategory?.subcategories.map((subcategory) => (
              <option key={subcategory.key} value={subcategory.key}>
                {subcategory.name}
              </option>
            ))}
          </select>
          {show(categoryError) ? <FieldError>{categoryError}</FieldError> : null}
          <p className="text-xs text-muted-foreground">
            Automatic uses your rules and the built-in ones. A line nothing recognises goes to Review.
          </p>
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor={`${ids}-notes`} className="text-xs text-muted-foreground">
            Notes
          </label>
          <textarea
            id={`${ids}-notes`}
            className="min-h-16 max-h-40 resize-y rounded-md border border-input bg-transparent px-2 py-1.5 text-sm text-foreground"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
          />
        </div>
      </DialogBody>
      <DialogFooter className="items-center">
        {submitError ? (
          <p role="alert" className="mr-auto min-w-0 flex-1 text-sm text-destructive [overflow-wrap:anywhere]">
            {submitError}
          </p>
        ) : null}
        <Button type="button" variant="outline" size="sm" disabled={pending} onClick={onClose}>
          Cancel
        </Button>
        <Button type="button" size="sm" disabled={pending} onClick={() => void submit()}>
          {pending ? "Adding…" : "Add transaction"}
        </Button>
      </DialogFooter>
    </Dialog>
  );
}

function FieldError({ children }: { children: React.ReactNode }) {
  return (
    <p role="alert" className="text-xs text-destructive">
      {children}
    </p>
  );
}
