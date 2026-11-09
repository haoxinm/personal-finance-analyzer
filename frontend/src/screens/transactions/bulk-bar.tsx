import * as React from "react";

import { Button } from "@/components/ui/button";

import type { Category, Kind } from "./types";

export interface BulkBarProps {
  selectedCount: number;
  totalMatching: number;
  loadedCount: number;
  categories: Category[];
  onSelectAllMatching: () => void;
  onClear: () => void;
  onApply: (patch: { categoryKey: string; subcategoryKey: string; kind?: Kind }) => void;
  applying?: boolean;
  /** Hand every selected row that was edited by hand back to automatic classification. */
  onRevert?: () => void;
  reverting?: boolean;
  selectingAll?: boolean;
  /** The selection is "everything the filters match" (not rows the user ticked): applying a label
   *  then asks for confirmation, with the count, first. */
  allMatchingSelected?: boolean;
}

/**
 * The bulk-relabel bar: appears once at least one row is selected, and applies one label to
 * every selected transaction via `POST /transactions/bulk-update` (§3.12). "Select all N
 * matching filters" resolves the *whole* filtered set into ids first (`useAllMatchingIds`) —
 * without it, a selection could only ever cover what has scrolled into memory so far.
 */
export function BulkBar({
  selectedCount,
  totalMatching,
  loadedCount,
  categories,
  onSelectAllMatching,
  onClear,
  onApply,
  applying,
  onRevert,
  reverting,
  selectingAll,
  allMatchingSelected,
}: BulkBarProps) {
  const [categoryKey, setCategoryKey] = React.useState("");
  const [subcategoryKey, setSubcategoryKey] = React.useState("");
  const [confirming, setConfirming] = React.useState(false);

  if (selectedCount === 0) return null;

  const selectedCategory = categories.find((c) => c.key === categoryKey);
  const canApply = categoryKey !== "" && subcategoryKey !== "" && !applying;
  const subcategoryName = selectedCategory?.subcategories.find((s) => s.key === subcategoryKey)?.name;

  if (confirming && selectedCategory) {
    return (
      <div
        role="alertdialog"
        aria-label="Confirm bulk update"
        className="flex flex-wrap items-center gap-3 rounded-lg border border-destructive/40 bg-destructive/5 p-3"
      >
        <span className="text-sm">
          Set{" "}
          <strong>
            {selectedCategory.name} / {subcategoryName}
          </strong>{" "}
          on all <strong>{selectedCount.toLocaleString()}</strong> matching transactions? This replaces
          their current category. Each one is edited once; no rule is created.
        </span>
        <Button
          type="button"
          size="sm"
          disabled={applying}
          onClick={() => {
            setConfirming(false);
            onApply({ categoryKey, subcategoryKey });
          }}
        >
          {applying ? "Applying…" : `Yes, apply to ${selectedCount.toLocaleString()}`}
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={() => setConfirming(false)}>
          Cancel
        </Button>
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-primary/30 bg-primary/5 p-3">
      <span className="text-sm font-medium">
        {allMatchingSelected ? `All ${selectedCount.toLocaleString()} matching selected` : `${selectedCount} selected`}
        {totalMatching > loadedCount && selectedCount < totalMatching ? (
          <Button type="button" variant="link" size="sm" onClick={onSelectAllMatching} disabled={selectingAll}>
            {selectingAll ? "Selecting…" : `Select all ${totalMatching} matching filters`}
          </Button>
        ) : null}
      </span>

      <select
        className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
        value={categoryKey}
        onChange={(e) => {
          setCategoryKey(e.target.value);
          setSubcategoryKey("");
        }}
        aria-label="Bulk category"
      >
        <option value="">Category…</option>
        {categories.map((category) => (
          <option key={category.key} value={category.key}>
            {category.name}
          </option>
        ))}
      </select>
      <select
        className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
        value={subcategoryKey}
        onChange={(e) => setSubcategoryKey(e.target.value)}
        disabled={!selectedCategory}
        aria-label="Bulk subcategory"
      >
        <option value="">Subcategory…</option>
        {selectedCategory?.subcategories.map((subcategory) => (
          <option key={subcategory.key} value={subcategory.key}>
            {subcategory.name}
          </option>
        ))}
      </select>

      <Button
        type="button"
        size="sm"
        disabled={!canApply}
        onClick={() => {
          if (allMatchingSelected) setConfirming(true);
          else onApply({ categoryKey, subcategoryKey });
        }}
      >
        {applying ? "Applying…" : `Apply to ${selectedCount}`}
      </Button>
      {onRevert ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={reverting || applying}
          onClick={onRevert}
          title="Remove your one-time edits from the selected transactions and let the rules decide again. Rows you never edited are left as they are."
        >
          {reverting ? "Reverting…" : "Revert to automatic"}
        </Button>
      ) : null}
      <Button type="button" variant="ghost" size="sm" onClick={onClear}>
        Clear selection
      </Button>
    </div>
  );
}
