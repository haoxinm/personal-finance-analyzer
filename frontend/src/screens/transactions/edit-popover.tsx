import * as React from "react";

import { Money } from "@/components/primitives/money";
import { SegmentedControl } from "@/components/primitives/segmented-control";
import { SuggestionChips } from "@/components/primitives/suggestion-chips";
import type { SuggestionCandidate } from "@/components/primitives/suggestion-labels";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

import { formatAmountInput, parseAmountInput } from "./amount-input";
import { isoDateFromToday } from "./dates";
import { KINDS, isExcluded, isManual, type Category, type Kind, type Transaction } from "./types";

export type CorrectableField = "posted_date" | "amount_minor" | "description_clean";

type Direction = "out" | "in";

const DIRECTION_OPTIONS = [
  { value: "out", label: "Money out" },
  { value: "in", label: "Money in" },
] as const;

export interface EditPopoverProps {
  transaction: Transaction;
  categories: Category[];
  /** Only the fields the user actually changed are present. */
  onSave: (patch: {
    categoryKey?: string;
    subcategoryKey?: string;
    kind?: Kind;
    notes?: string;
    /** Corrected values (the imported value is kept by the server and can be restored). */
    postedDate?: string;
    descriptionClean?: string;
    /** Signed minor units: positive = money out, negative = money in (I5). */
    amountMinor?: number;
    /** Fields put back to their imported value. Never also present as a value. */
    revertFields?: CorrectableField[];
    /** Only present (and true) when "Also create a rule" was ticked. */
    createRule?: boolean;
  }) => void;
  onClose: () => void;
  saving?: boolean;
  /** What each classification layer would say about this transaction (the candidates endpoint),
   * once loaded; the chips render only when given. */
  candidates?: SuggestionCandidate[];
  /** The candidates disagree: the chips explain it. */
  conflict?: boolean;
  /** One click on a chip: apply that candidate as a one-time edit (the caller closes the dialog). */
  onApplySuggestion?: (candidate: SuggestionCandidate) => void;
  /** Hand an edited row back to automatic classification (the caller closes the dialog). Offered
   * only when the row carries a one-time edit, which the candidates say. */
  onRevert?: () => void;
  /** The last save failed: shown in the footer, beside the buttons (a toast alone can be missed). */
  saveError?: string | null;
  /** Leave this row out of every total (or, when it already is, count it again). The caller
   * performs the change and closes the dialog. Offered for every row. */
  onToggleExcluded?: () => void;
  /** Delete this row for good. Offered only for rows added by hand; the caller closes the dialog
   * on success. Parsed rows cannot be deleted (a re-import would bring them back): exclude them. */
  onDelete?: () => void;
  /** A delete is in flight. */
  deleting?: boolean;
}

/**
 * The transactions grid's inline editor: change a transaction's category, subcategory and
 * `kind` in place, without navigating away (Review, P3-B, is the dedicated queue for *working
 * through* `needs_review` transactions; this is the quick single-row correction for a grid
 * the user is already scanning). Saving goes through `PATCH /transactions/{id}` (§3.12) via
 * the shared `usePatchTransaction` hook. The edit is a one-time patch of this transaction only:
 * it always wins over rules and re-runs, and nothing is remembered about the merchant. The
 * optional "Also create a rule" checkbox (off by default) is the explicit merchant-level action:
 * it sends `create_rule`, which saves an exact rule for the merchant's future transactions.
 *
 * Above the form, when the transaction has them, one-click suggestion chips show what each
 * classification layer would say (`SuggestionChips`): the higher-priority answer is already
 * applied, and a chip applies a lower-priority one as a one-time edit. A row that was edited by
 * hand also offers "Revert to automatic" (`onRevert`), the one way to let rules reach it again.
 */
export function EditPopover({
  transaction,
  categories,
  onSave,
  onClose,
  saving,
  candidates,
  conflict = false,
  onApplySuggestion,
  onRevert,
  saveError,
  onToggleExcluded,
  onDelete,
  deleting,
}: EditPopoverProps) {
  const ids = React.useId();
  const manual = isManual(transaction);
  const excluded = isExcluded(transaction);
  // What the restore controls call the first value: a hand-added row was never "imported".
  const original = manual ? "original" : "imported";
  const [confirmingDiscard, setConfirmingDiscard] = React.useState(false);
  const [confirmingDelete, setConfirmingDelete] = React.useState(false);
  const keepEditingRef = React.useRef<HTMLButtonElement>(null);
  // Move focus to the safe choice when the confirmation appears (its buttons replace Cancel/Save).
  React.useEffect(() => {
    if (confirmingDiscard || confirmingDelete) keepEditingRef.current?.focus();
  }, [confirmingDiscard, confirmingDelete]);
  const [categoryKey, setCategoryKey] = React.useState(transaction.category_key ?? "");
  const [subcategoryKey, setSubcategoryKey] = React.useState(transaction.subcategory_key ?? "");
  const [kind, setKind] = React.useState<Kind>(transaction.kind);
  const [notes, setNotes] = React.useState(transaction.notes ?? "");
  const [createRule, setCreateRule] = React.useState(false);
  const [postedDate, setPostedDate] = React.useState(transaction.posted_date);
  const [description, setDescription] = React.useState(transaction.description_clean);
  const [direction, setDirection] = React.useState<Direction>(
    transaction.amount_minor < 0 ? "in" : "out",
  );
  const [amountText, setAmountText] = React.useState(formatAmountInput(transaction.amount_minor));

  const selectedCategory = categories.find((c) => c.key === categoryKey);
  const corrections = transaction.corrections ?? null;

  React.useEffect(() => {
    // Reset the subcategory whenever the category changes to one that doesn't have it,
    // rather than silently submitting a mismatched pair.
    if (selectedCategory && !selectedCategory.subcategories.some((s) => s.key === subcategoryKey)) {
      setSubcategoryKey("");
    }
  }, [selectedCategory, subcategoryKey]);

  // Each field saves on its own: a kind-only or notes-only edit needs no category, but a category
  // change needs both halves of the pair.
  const categoryChanged =
    categoryKey !== (transaction.category_key ?? "") ||
    subcategoryKey !== (transaction.subcategory_key ?? "");
  const categoryComplete = categoryKey !== "" && subcategoryKey !== "";
  const kindChanged = kind !== transaction.kind;
  const notesChanged = notes !== (transaction.notes ?? "");

  // Details (corrections). Each is validated on its own; an invalid one blocks Save.
  const dateError = postedDate === "" ? "Enter a valid date." : null;
  const trimmedDescription = description.trim();
  const descriptionError =
    trimmedDescription === "" ? "Enter a description." : null;
  const parsedAmount = parseAmountInput(amountText);
  const amountError =
    parsedAmount === null
      ? "Enter an amount above 0, with at most 2 decimals (for example 12.50)."
      : null;
  const signedAmount =
    parsedAmount === null ? null : direction === "out" ? parsedAmount : -parsedAmount;

  const dateChanged = dateError === null && postedDate !== transaction.posted_date;
  const descriptionChanged =
    descriptionError === null && trimmedDescription !== transaction.description_clean;
  const amountChanged = signedAmount !== null && signedAmount !== transaction.amount_minor;
  const detailsValid = dateError === null && descriptionError === null && amountError === null;

  const importedDate = corrections?.posted_date ?? null;
  const importedDescription = corrections?.description_clean ?? null;
  const importedAmount = corrections?.amount_minor ?? null;

  const canSave =
    detailsValid &&
    (categoryChanged ? categoryComplete : true) &&
    ((categoryChanged && categoryComplete) ||
      kindChanged ||
      notesChanged ||
      dateChanged ||
      descriptionChanged ||
      amountChanged);
  // A rule needs a merchant to match and a category to apply, so the box only works alongside a
  // complete category change.
  const merchant = transaction.merchant_key ?? "";
  const canCreateRule = merchant !== "" && categoryChanged && categoryComplete;
  const ruleRequested = createRule && canCreateRule;

  function buildPatch(): Parameters<EditPopoverProps["onSave"]>[0] {
    const revertFields: CorrectableField[] = [];
    const patch: Parameters<EditPopoverProps["onSave"]>[0] = {
      ...(categoryChanged && categoryComplete ? { categoryKey, subcategoryKey } : {}),
      ...(kindChanged ? { kind } : {}),
      ...(notesChanged ? { notes } : {}),
      ...(ruleRequested ? { createRule: true } : {}),
    };
    // Editing a corrected field back to its imported value is a revert, not a new correction.
    if (dateChanged) {
      if (importedDate !== null && postedDate === importedDate) revertFields.push("posted_date");
      else patch.postedDate = postedDate;
    }
    if (descriptionChanged) {
      if (importedDescription !== null && trimmedDescription === importedDescription)
        revertFields.push("description_clean");
      else patch.descriptionClean = trimmedDescription;
    }
    if (amountChanged && signedAmount !== null) {
      if (importedAmount !== null && signedAmount === importedAmount)
        revertFields.push("amount_minor");
      else patch.amountMinor = signedAmount;
    }
    if (revertFields.length > 0) patch.revertFields = revertFields;
    return patch;
  }

  // Anything typed or picked that differs from what the dialog opened with (even if invalid).
  const dirty =
    categoryChanged ||
    kindChanged ||
    notesChanged ||
    createRule ||
    postedDate !== transaction.posted_date ||
    description !== transaction.description_clean ||
    amountText !== formatAmountInput(transaction.amount_minor) ||
    direction !== (transaction.amount_minor < 0 ? "in" : "out");

  // Escape, a press outside and Cancel never throw away edits silently: ask first, in the footer.
  function requestClose() {
    if (saving || deleting) return; // a save in flight: Escape / outside click must not close the dialog
    if (confirmingDelete) setConfirmingDelete(false);
    else if (confirmingDiscard) setConfirmingDiscard(false);
    else if (dirty) setConfirmingDiscard(true);
    else onClose();
  }

  const directionChanged = direction !== (transaction.amount_minor < 0 ? "in" : "out");
  const suggestRefund = directionChanged && direction === "in" && (kind === "purchase" || kind === "fee");

  const showRaw = transaction.description_raw !== transaction.description_clean;
  const fieldClass =
    "h-9 rounded-md border border-input bg-transparent px-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring aria-[invalid=true]:border-destructive";
  const selectClass = "h-9 rounded-md border border-input bg-transparent px-2 text-sm";

  return (
    <Dialog onClose={onClose} onRequestClose={requestClose} aria-labelledby={`${ids}-title`}>
      <DialogHeader>
        <DialogTitle id={`${ids}-title`}>Edit transaction</DialogTitle>
        <DialogDescription data-testid="edit-summary">
          <div className="flex items-start justify-between gap-3">
            {/* Clamped: a very long description must not eat the scrolling body's space. The
                full text is in the Description field below and in the tooltip. */}
            <div
              className="line-clamp-3 min-w-0"
              title={`${transaction.posted_date} · ${transaction.description_clean}`}
            >
              <span className="whitespace-nowrap">{transaction.posted_date}</span>
              {" · "}
              <span className="text-foreground">{transaction.description_clean}</span>
            </div>
            <Money
              minorUnits={transaction.amount_minor}
              currency={transaction.currency}
              className="shrink-0 font-semibold"
            />
          </div>
          {showRaw ? (
            <p className="mt-1 line-clamp-2 break-all font-mono text-xs" title={transaction.description_raw}>
              As printed: {transaction.description_raw}
            </p>
          ) : null}
        </DialogDescription>
      </DialogHeader>
      <DialogBody>
        {candidates && onApplySuggestion ? (
          <SuggestionChips
            candidates={candidates}
            conflict={conflict}
            categories={categories}
            onApply={onApplySuggestion}
            onRevert={onRevert}
            disabled={saving}
          />
        ) : null}
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          Category
          <select
            className={selectClass}
            value={categoryKey}
            onChange={(e) => setCategoryKey(e.target.value)}
          >
            <option value="">Choose a category</option>
            {categories.map((category) => (
              <option key={category.key} value={category.key}>
                {category.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          Subcategory
          <select
            className={selectClass}
            value={subcategoryKey}
            onChange={(e) => setSubcategoryKey(e.target.value)}
            disabled={!selectedCategory}
          >
            <option value="">Choose a subcategory</option>
            {selectedCategory?.subcategories.map((subcategory) => (
              <option key={subcategory.key} value={subcategory.key}>
                {subcategory.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          Kind
          <select
            className={selectClass}
            value={kind}
            onChange={(e) => setKind(e.target.value as Kind)}
          >
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>

        <fieldset className="flex min-w-0 flex-col gap-3 rounded-md border border-border p-3">
          <legend className="px-1 text-xs font-medium text-foreground">Details</legend>
          <p className="text-xs text-muted-foreground">
            {manual
              ? "Change what you entered. The original value is kept, so you can restore it."
              : "Fix something the statement parser got wrong. The imported value is kept, so you can restore it."}
          </p>

          <div className="flex flex-col gap-1">
            <label htmlFor={`${ids}-date`} className="text-xs text-muted-foreground">
              Date
            </label>
            <input
              id={`${ids}-date`}
              type="date"
              className={fieldClass}
              value={postedDate}
              min="1970-01-01"
              max={isoDateFromToday(366)}
              aria-invalid={dateError !== null}
              aria-describedby={dateError ? `${ids}-date-err` : undefined}
              onChange={(e) => setPostedDate(e.target.value)}
            />
            {dateError ? (
              <FieldError id={`${ids}-date-err`}>{dateError}</FieldError>
            ) : null}
            {importedDate !== null && postedDate !== importedDate ? (
              <ImportedAs label="date" word={original} onRestore={() => setPostedDate(importedDate)}>
                {importedDate}
              </ImportedAs>
            ) : importedDate !== null ? (
              <p className="text-xs text-muted-foreground">Restored to the {original} date.</p>
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
              className={fieldClass}
              value={description}
              aria-invalid={descriptionError !== null}
              aria-describedby={`${descriptionError ? `${ids}-desc-err ` : ""}${ids}-desc-help`}
              onChange={(e) => setDescription(e.target.value)}
            />
            {descriptionError ? (
              <FieldError id={`${ids}-desc-err`}>{descriptionError}</FieldError>
            ) : null}
            <p id={`${ids}-desc-help`} className="text-xs text-muted-foreground">
              {merchant !== ""
                ? `Rules still match this merchant as imported (‘${merchant}’).`
                : "Only the name shown here changes."}
            </p>
            {importedDescription !== null && trimmedDescription !== importedDescription ? (
              <ImportedAs
                label="description"
                word={original}
                onRestore={() => setDescription(importedDescription)}
              >
                {importedDescription}
              </ImportedAs>
            ) : importedDescription !== null ? (
              <p className="text-xs text-muted-foreground">Restored to the {original} description.</p>
            ) : null}
          </div>

          <div className="flex flex-col gap-1">
            <span id={`${ids}-amount-label`} className="text-xs text-muted-foreground">
              Amount ({transaction.currency})
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
                className={`${fieldClass} w-32 min-w-0 flex-1 text-right tabular-nums`}
                value={amountText}
                aria-invalid={amountError !== null}
                aria-describedby={`${amountError ? `${ids}-amount-err ` : ""}${ids}-amount-help`}
                onChange={(e) => setAmountText(e.target.value)}
              />
            </div>
            {amountError ? (
              <FieldError id={`${ids}-amount-err`}>{amountError}</FieldError>
            ) : null}
            {suggestRefund ? (
              <p className="text-xs text-muted-foreground" data-testid="refund-hint">
                Money in on a {kind} is usually a refund.{" "}
                <button
                  type="button"
                  className="rounded-sm text-primary underline underline-offset-2 hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  onClick={() => setKind("refund")}
                >
                  Set kind to refund
                </button>
              </p>
            ) : null}
            <p id={`${ids}-amount-help`} className="text-xs text-muted-foreground">
              {direction === "out"
                ? "Money out: a purchase, fee or payment you made. "
                : "Money in: a refund, deposit or credit. "}
              {signedAmount !== null ? (
                <>
                  Saved as{" "}
                  <Money minorUnits={signedAmount} currency={transaction.currency} />.
                </>
              ) : null}
            </p>
            {importedAmount !== null && signedAmount !== importedAmount ? (
              <ImportedAs
                label="amount"
                word={original}
                onRestore={() => {
                  setDirection(importedAmount < 0 ? "in" : "out");
                  setAmountText(formatAmountInput(importedAmount));
                }}
              >
                <Money minorUnits={importedAmount} currency={transaction.currency} />
              </ImportedAs>
            ) : importedAmount !== null ? (
              <p className="text-xs text-muted-foreground">Restored to the {original} amount.</p>
            ) : null}
          </div>
        </fieldset>

        {onToggleExcluded ? (
          <div
            className="flex flex-col gap-2 rounded-md border border-border p-3"
            data-testid="inclusion-section"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-xs font-medium text-foreground">
                {excluded ? "Excluded from totals" : "Counted in totals"}
              </span>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={saving || deleting || dirty}
                onClick={onToggleExcluded}
              >
                {excluded ? "Include again" : "Exclude from totals"}
              </Button>
            </div>
            {dirty ? (
              <p className="text-xs text-amber-700 dark:text-amber-300" data-testid="inclusion-dirty-hint">
                Save or discard your changes first, then {excluded ? "include" : "exclude"} this row.
              </p>
            ) : null}
            <p className="text-xs text-muted-foreground">
              {excluded
                ? "This row stays on file but is left out of every total, chart and forecast."
                : "Excluding keeps the row on file but leaves it out of every total, chart and forecast, for example a duplicate or a line that is not yours. You can include it again at any time."}
            </p>
          </div>
        ) : null}

        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          Notes
          <textarea
            className="min-h-16 max-h-40 resize-y rounded-md border border-input bg-transparent px-2 py-1.5 text-sm text-foreground"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
          />
        </label>
        <p className="text-xs text-muted-foreground" data-testid="edit-scope-note">
          This change applies to this transaction only. Rules and re-classifying will not change it.
        </p>
        {merchant !== "" ? (
          <label className="flex items-start gap-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              className="mt-0.5 shrink-0"
              checked={ruleRequested}
              disabled={!canCreateRule}
              onChange={(e) => setCreateRule(e.target.checked)}
            />
            <span className="min-w-0 [overflow-wrap:anywhere]">
              Also create a rule for “{merchant}”: future transactions from this merchant get this
              category. Existing ones change only if you apply the rule on the Rules screen.
            </span>
          </label>
        ) : null}
      </DialogBody>
      <DialogFooter className="items-center">
        {confirmingDelete ? (
          <div
            role="alertdialog"
            aria-label="Delete this transaction?"
            className="flex w-full flex-wrap items-center justify-end gap-2"
          >
            <p className="mr-auto text-sm">Delete this transaction for good? This can’t be undone.</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              ref={keepEditingRef}
              onClick={() => setConfirmingDelete(false)}
            >
              Keep it
            </Button>
            <Button
              type="button"
              variant="destructive"
              size="sm"
              disabled={deleting}
              onClick={() => {
                setConfirmingDelete(false);
                onDelete?.();
              }}
            >
              {deleting ? "Deleting…" : "Delete transaction"}
            </Button>
          </div>
        ) : confirmingDiscard ? (
          <div
            role="alertdialog"
            aria-label="Discard changes?"
            className="flex w-full flex-wrap items-center justify-end gap-2"
          >
            <p className="mr-auto text-sm">Discard your unsaved changes?</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              ref={keepEditingRef}
              onClick={() => setConfirmingDiscard(false)}
            >
              Keep editing
            </Button>
            <Button type="button" variant="destructive" size="sm" onClick={onClose}>
              Discard changes
            </Button>
          </div>
        ) : (
          <>
            {saveError ? (
              <p role="alert" className="mr-auto min-w-0 flex-1 text-sm text-destructive [overflow-wrap:anywhere]">
                {saveError}
              </p>
            ) : null}
            {manual && onDelete ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="mr-auto text-destructive hover:text-destructive"
                disabled={saving || deleting}
                onClick={() => setConfirmingDelete(true)}
              >
                Delete…
              </Button>
            ) : null}
            <Button type="button" variant="outline" size="sm" onClick={requestClose}>
              Cancel
            </Button>
            <Button
              type="button"
              size="sm"
              disabled={!canSave || saving}
              onClick={() => onSave(buildPatch())}
            >
              {saving ? "Saving…" : "Save"}
            </Button>
          </>
        )}
      </DialogFooter>
    </Dialog>
  );
}

function FieldError({ id, children }: { id: string; children: React.ReactNode }) {
  return (
    <p id={id} role="alert" className="text-xs text-destructive">
      {children}
    </p>
  );
}

/** "Imported as X · Restore": shown only for a field the user corrected. */
function ImportedAs({
  label,
  word,
  onRestore,
  children,
}: {
  label: string;
  word: "imported" | "original";
  onRestore: () => void;
  children: React.ReactNode;
}) {
  return (
    <p className="text-xs text-muted-foreground">
      {word === "original" ? "Originally" : "Imported as"} <span className="text-foreground">{children}</span> ·{" "}
      <button
        type="button"
        className="rounded-sm text-primary underline underline-offset-2 hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        aria-label={`Restore ${word} ${label}`}
        onClick={onRestore}
      >
        Restore
      </button>
    </p>
  );
}
