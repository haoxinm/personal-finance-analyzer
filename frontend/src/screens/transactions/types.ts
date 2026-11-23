import type { components } from "@/api/client";

export type Transaction = components["schemas"]["Transaction"];
export type Kind = components["schemas"]["Kind"];
export type Category = components["schemas"]["Category"];
export type Subcategory = components["schemas"]["Subcategory"];

export const KINDS: Kind[] = [
  "purchase",
  "refund",
  "payment",
  "transfer",
  "fee",
  "interest",
  "adjustment",
];

/** A row that sits out of totals: shown muted and struck through when the grid includes it. */
export function isExcluded(txn: Transaction): boolean {
  return txn.excluded === true;
}

export function isManual(txn: Transaction): boolean {
  return txn.source === "manual";
}
