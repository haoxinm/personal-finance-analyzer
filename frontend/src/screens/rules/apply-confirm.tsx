import { Button } from "@/components/ui/button";

import type { RuleApplyPreview } from "./hooks";

export interface PendingApply {
  id: number;
  pattern: string;
  kind: string | null;
  preview: RuleApplyPreview;
}

function plural(count: number, noun: string): string {
  return `${count.toLocaleString()} ${noun}${count === 1 ? "" : "s"}`;
}

export function ApplyConfirm({
  pending,
  busy,
  onConfirm,
  onCancel,
}: {
  pending: PendingApply;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const { preview } = pending;
  return (
    <div
      role="alertdialog"
      aria-label="Confirm apply rule"
      className="flex flex-col gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm"
    >
      <p>
        Apply &ldquo;{pending.pattern}&rdquo; to <strong>{plural(preview.matches, "existing transaction")}</strong>
        ? Each one gets the rule&rsquo;s category and is no longer flagged for review.
      </p>
      {preview.all_users ? (
        <p data-testid="apply-scope">
          This rule has no owner, so it applies to every user&rsquo;s transactions
          {preview.user_count > 1 ? ` (these belong to ${preview.user_count} users)` : ""}.
        </p>
      ) : preview.user_count > 1 ? (
        <p data-testid="apply-scope">These belong to {preview.user_count} users.</p>
      ) : null}
      {preview.kind_changes > 0 ? (
        <p data-testid="apply-kind">
          {plural(preview.kind_changes, "transaction")} will also change kind
          {pending.kind ? ` to ${pending.kind}` : ""}, which moves them in or out of spend totals.
        </p>
      ) : null}
      <p className="text-muted-foreground">
        Transactions you categorized yourself are never changed. You can undo this right after.
      </p>
      <div className="flex gap-2">
        <Button type="button" size="sm" disabled={busy} onClick={onConfirm}>
          {busy ? "Applying…" : `Yes, apply to ${preview.matches.toLocaleString()}`}
        </Button>
        <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
