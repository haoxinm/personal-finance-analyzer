import * as React from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/utils";

import { formatPageRanges, parsePageRanges } from "./page-ranges";

const THUMB_WIDTH = 160;

export interface PagePickerPage {
  pageNumber: number;
  charCount?: number;
  /** Tooltip, e.g. the first words of the page. */
  title?: string;
  /** When set the page cannot be selected and this says why. */
  disabledReason?: string | null;
  /** Mark the page as the one the app would pick by itself. */
  suggested?: boolean;
}

export interface PagePickerProps {
  /** Number of pages in the document; pages are 1..total. */
  total: number;
  /** Optional per-page details (character count, tooltip, disabled reason). */
  pages?: readonly PagePickerPage[];
  /** The selected page numbers (controlled). */
  selected: readonly number[];
  /** Called with the new selection, sorted ascending. */
  onChange: (pages: number[]) => void;
  /** URL of a page thumbnail (`<img>` cannot send headers; the caller adds any token). */
  imageUrl: (pageNumber: number, width: number) => string;
  /** Shown (as an alert) while nothing is selected. */
  emptyMessage?: string;
  /** Section title; defaults to "Pages". */
  label?: string;
  /** Hide the "1-3,5" range input. */
  showRangeInput?: boolean;
  className?: string;
}

/**
 * Page thumbnails with checkboxes: select all / none, a range input, a clear selected state
 * (highlighted card + "Selected" mark + the count in the header) and lazy-loaded thumbnails in a
 * fixed-height scroll area. Used for card statements and tax documents.
 */
export function PagePicker({
  total,
  pages = [],
  selected,
  onChange,
  imageUrl,
  emptyMessage = "Select at least one page.",
  label = "Pages",
  showRangeInput = true,
  className,
}: PagePickerProps) {
  const allPages = React.useMemo(() => Array.from({ length: total }, (_, i) => i + 1), [total]);
  const info = React.useMemo(() => new Map(pages.map((p) => [p.pageNumber, p])), [pages]);
  const selectable = React.useMemo(
    () => allPages.filter((n) => !info.get(n)?.disabledReason),
    [allPages, info],
  );
  const [rangeText, setRangeText] = React.useState<string | null>(null);
  const chosen = React.useMemo(() => new Set(selected), [selected]);

  const apply = (next: number[], syncText = true) => {
    onChange([...next].sort((a, b) => a - b));
    if (syncText) setRangeText(null);
  };
  const allSelected = selectable.length > 0 && selectable.every((n) => chosen.has(n));
  const shownText = rangeText ?? (allSelected ? "" : formatPageRanges([...selected]));
  const parsedRange = rangeText === null ? null : parsePageRanges(rangeText, total);
  const invalidRange = rangeText !== null && rangeText.trim() !== "" && parsedRange === null;

  return (
    <div className={cn("flex flex-col gap-2", className)}>
      <div className="flex flex-wrap items-center gap-2 text-xs font-medium text-muted-foreground">
        <span data-testid="page-picker-count">
          {label} ({selected.length} of {total} selected)
        </span>
        <Button type="button" size="sm" variant="outline" onClick={() => apply(selectable)}>
          All
        </Button>
        <Button type="button" size="sm" variant="outline" onClick={() => apply([])}>
          None
        </Button>
        {showRangeInput ? (
          <Input
            aria-label="Page ranges"
            placeholder="e.g. 1-3,5 (blank = all)"
            className="h-8 w-48 text-sm"
            aria-invalid={invalidRange}
            value={shownText}
            onChange={(event) => {
              const text = event.target.value;
              setRangeText(text);
              if (text.trim() === "") {
                apply(selectable, false);
                return;
              }
              const parsed = parsePageRanges(text, total);
              if (parsed) apply(parsed.filter((n) => selectable.includes(n)), false);
            }}
          />
        ) : null}
      </div>
      {selected.length === 0 ? (
        <p role="alert" className="text-xs text-destructive">
          {emptyMessage}
        </p>
      ) : null}
      <div
        className="grid max-h-96 grid-cols-2 gap-2 overflow-y-auto sm:grid-cols-4 lg:grid-cols-6"
        data-testid="page-picker-grid"
      >
        {allPages.map((pageNumber) => {
          const page = info.get(pageNumber);
          const disabled = Boolean(page?.disabledReason);
          const checked = chosen.has(pageNumber) && !disabled;
          return (
            <label
              key={pageNumber}
              title={disabled ? (page?.disabledReason ?? undefined) : page?.title}
              data-selected={checked}
              data-disabled={disabled}
              className={cn(
                "relative flex flex-col gap-1 rounded-md border p-1.5 text-xs transition-colors",
                disabled ? "cursor-not-allowed border-dashed border-input opacity-50" : "cursor-pointer",
                checked ? "border-primary bg-primary/10 ring-2 ring-primary" : "border-input",
              )}
            >
              <img
                src={imageUrl(pageNumber, THUMB_WIDTH)}
                alt={`Page ${pageNumber} preview`}
                loading="lazy"
                width={THUMB_WIDTH}
                className={cn("w-full bg-muted", !checked && !disabled && "opacity-70")}
              />
              {checked ? (
                <span
                  aria-hidden="true"
                  className="absolute right-2 top-2 rounded bg-primary px-1.5 py-0.5 text-xs font-semibold text-primary-foreground"
                >
                  ✓ Selected
                </span>
              ) : null}
              <span className="flex flex-wrap items-center gap-1.5">
                <input
                  type="checkbox"
                  aria-label={`Page ${pageNumber}`}
                  checked={checked}
                  disabled={disabled}
                  onChange={() =>
                    apply(checked ? selected.filter((n) => n !== pageNumber) : [...selected, pageNumber])
                  }
                />
                Page {pageNumber}
                {page?.charCount !== undefined ? (
                  <span className="text-muted-foreground">· {page.charCount} chars</span>
                ) : null}
                {page?.suggested ? <span className="text-muted-foreground">· suggested</span> : null}
              </span>
              {disabled ? <span className="text-muted-foreground">{page?.disabledReason}</span> : null}
            </label>
          );
        })}
      </div>
    </div>
  );
}
