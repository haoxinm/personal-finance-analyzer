import * as React from "react";

import { Button } from "@/components/ui/button";

import { KINDS, type Category, type Kind, type Transaction } from "./types";

export interface EditPopoverProps {
  transaction: Transaction;
  categories: Category[];
  /** Only the fields the user actually changed are present. */
  onSave: (patch: {
    categoryKey?: string;
    subcategoryKey?: string;
    kind?: Kind;
    notes?: string;
    /** Only present (and true) when "Also create a rule" was ticked. */
    createRule?: boolean;
  }) => void;
  onClose: () => void;
  saving?: boolean;
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
 */
export function EditPopover({ transaction, categories, onSave, onClose, saving }: EditPopoverProps) {
  const [categoryKey, setCategoryKey] = React.useState(transaction.category_key ?? "");
  const [subcategoryKey, setSubcategoryKey] = React.useState(transaction.subcategory_key ?? "");
  const [kind, setKind] = React.useState<Kind>(transaction.kind);
  const [notes, setNotes] = React.useState(transaction.notes ?? "");
  const [createRule, setCreateRule] = React.useState(false);

  const selectedCategory = categories.find((c) => c.key === categoryKey);

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
  const canSave =
    (categoryChanged ? categoryComplete : true) &&
    ((categoryChanged && categoryComplete) || kindChanged || notesChanged);
  // A rule needs a merchant to match and a category to apply, so the box only works alongside a
  // complete category change.
  const merchant = transaction.merchant_key ?? "";
  const canCreateRule = merchant !== "" && categoryChanged && categoryComplete;
  const ruleRequested = createRule && canCreateRule;

  // Keyboard users: focus moves into the dialog, Escape closes it, and focus goes back to the
  // control that opened it (the row's Edit button) when it unmounts.
  const dialogRef = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialogRef.current?.querySelector<HTMLElement>("select, textarea, button")?.focus();
    return () => opener?.focus();
  }, []);

  return (
    <div
      className="fixed inset-0 z-20 flex items-center justify-center bg-black/30 p-4"
      onClick={onClose}
    >
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label={`Edit transaction ${transaction.id}`}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        if (e.key === "Escape") onClose();
      }}
      className="flex w-80 flex-col gap-3 rounded-md border border-border bg-background p-4 shadow-lg"
    >
      <label className="flex flex-col gap-1 text-xs text-muted-foreground">
        Category
        <select
          className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
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
          className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
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
          className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
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
      <label className="flex flex-col gap-1 text-xs text-muted-foreground">
        Notes
        <textarea
          className="min-h-16 rounded-md border border-input bg-transparent px-2 py-1.5 text-sm text-foreground"
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
            className="mt-0.5"
            checked={ruleRequested}
            disabled={!canCreateRule}
            onChange={(e) => setCreateRule(e.target.checked)}
          />
          <span>
            Also create a rule for “{merchant}”: future transactions from this merchant get this
            category. Existing ones change only if you apply the rule on the Rules screen.
          </span>
        </label>
      ) : null}
      <div className="flex justify-end gap-2">
        <Button type="button" variant="outline" size="sm" onClick={onClose}>
          Cancel
        </Button>
        <Button
          type="button"
          size="sm"
          disabled={!canSave || saving}
          onClick={() =>
            onSave({
              ...(categoryChanged && categoryComplete ? { categoryKey, subcategoryKey } : {}),
              ...(kindChanged ? { kind } : {}),
              ...(notesChanged ? { notes } : {}),
              ...(ruleRequested ? { createRule: true } : {}),
            })
          }
        >
          {saving ? "Saving…" : "Save"}
        </Button>
      </div>
    </div>
    </div>
  );
}
