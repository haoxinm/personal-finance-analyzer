import { Link } from "react-router-dom";

import { Button } from "@/components/ui/button";

import type { ImportItem } from "./types";

export interface SummaryBarProps {
  items: ImportItem[];
}

/** The "3 statements imported · 412 new transactions · 38 need review" line the plan asks for,
 * plus "· 12 already present" (rows an overlapping statement had provided) and "· 1 already
 * imported" (a file dropped twice), and the next-step buttons. Renders nothing until at least one
 * statement has finished importing. The needs-review clause is shown only once at least one
 * parsed item has a count (`fetchNeedsReviewCount`'s best-effort result); see that function's
 * docstring for why the count can lag or be approximate. */
export function SummaryBar({ items }: SummaryBarProps) {
  const parsedItems = items.filter((item) => item.status === "parsed");
  if (parsedItems.length === 0) return null;
  const parsed = parsedItems.filter((item) => !item.duplicate);
  const alreadyImported = parsedItems.length - parsed.length;

  const newTransactions = parsed.reduce(
    (sum, item) => sum + (item.insertedCount ?? item.statement?.txn_count ?? 0),
    0,
  );
  const alreadyPresent = parsed.reduce((sum, item) => sum + (item.skippedDuplicates ?? 0), 0);
  const knownReviewCounts = parsed.filter((item) => item.needsReviewCount !== undefined);
  const needsReview = knownReviewCounts.reduce((sum, item) => sum + (item.needsReviewCount ?? 0), 0);

  const parts: string[] = [];
  if (parsed.length > 0) {
    parts.push(
      `${parsed.length} statement${parsed.length === 1 ? "" : "s"} imported`,
      `${newTransactions} new transaction${newTransactions === 1 ? "" : "s"}`,
    );
    if (alreadyPresent > 0) parts.push(`${alreadyPresent} already present`);
  }
  if (alreadyImported > 0) parts.push(`${alreadyImported} already imported`);
  if (parsed.length > 0 && knownReviewCounts.length === parsed.length) {
    parts.push(`${needsReview} need review`);
  }

  return (
    <div className="flex flex-wrap items-center gap-3">
      <p className="text-sm font-medium text-foreground" role="status">
        {parts.join(" · ")}
      </p>
      {needsReview > 0 ? (
        <Button asChild size="sm">
          <Link to="/spending/review">Review {needsReview} &rarr;</Link>
        </Button>
      ) : null}
      <Button asChild size="sm" variant="outline">
        <Link to="/spending/transactions">View transactions</Link>
      </Button>
    </div>
  );
}
