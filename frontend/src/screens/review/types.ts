import type { components } from "@/api/client";

export type Transaction = components["schemas"]["Transaction"];
export type Category = components["schemas"]["Category"];
export type Subcategory = components["schemas"]["Subcategory"];
export type AcceptUndoToken = components["schemas"]["AcceptUndo"];

/** A flattened `(category, subcategory)` pair, for the numbered quick-pick list (§P3-B). */
export interface CategoryChoice {
  categoryKey: string;
  categoryLabel: string;
  subcategoryKey: string;
  subcategoryLabel: string;
}

/** One entry of the undo stack: the server's undo token for an accept (it restores every row's
 * category, `needs_review` flag, kind and provenance, and deletes or restores the merchant's rule
 * if the accept was an "always categorize" one) plus the queue rows the accept removed, so undo
 * can put them back in place. */
export interface UndoEntry {
  undo: AcceptUndoToken;
  /** Rows the accept took out of the visible queue, in ascending original position. */
  removed: Array<{ queueIndex: number; transaction: Transaction }>;
}
