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
  }) => void;
  onClose: () => void;
  saving?: boolean;
}

/**
 * The transactions grid's inline editor: change a transaction's category, subcategory and
 * `kind` in place, without navigating away (Review, P3-B, is the dedicated queue for *working
 * through* `needs_review` transactions; this is the quick single-row correction for a grid
 * the user is already scanning). Saving goes through `PATCH /transactions/{id}` (§3.12) via
 * the shared `usePatchTransaction` hook, which — per that endpoint's contract — always writes
 * `merchant_map(source='user')` (I6).
 */
export function EditPopover({ transaction, categories, onSave, onClose, saving }: EditPopoverProps) {
  const [categoryKey, setCategoryKey] = React.useState(transaction.category_key ?? "");
  const [subcategoryKey, setSubcategoryKey] = React.useState(transaction.subcategory_key ?? "");
  const [kind, setKind] = React.useState<Kind>(transaction.kind);
  const [notes, setNotes] = React.useState(transaction.notes ?? "");

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

  return (
    <div
      className="fixed inset-0 z-20 flex items-center justify-center bg-black/30 p-4"
      onClick={onClose}
    >
    <div
      role="dialog"
      aria-label={`Edit transaction ${transaction.id}`}
      onClick={(e) => e.stopPropagation()}
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
