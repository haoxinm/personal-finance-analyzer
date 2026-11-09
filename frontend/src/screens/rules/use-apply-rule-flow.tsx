import * as React from "react";

import { useToast } from "@/components/ui/use-toast";

import { ApplyConfirm, type PendingApply } from "./apply-confirm";
import { useApplyRule, useRuleApplyPreview, useUndoApply } from "./hooks";

function plural(count: number, noun: string): string {
  return `${count.toLocaleString()} ${noun}${count === 1 ? "" : "s"}`;
}

/** What a rule's `Apply` reported, as a toast title. */
function applyResultTitle(updated: number): string {
  return updated === 0
    ? "Nothing to update"
    : `Updated ${updated} existing transaction${updated === 1 ? "" : "s"}`;
}

/**
 * "Apply to existing" as a three-step flow instead of one click: look up what it would change,
 * confirm with the count (and any cross-user or kind effect), apply, then offer an Undo in the
 * result toast. `dialog` is the confirmation to render next to the button that called `start`.
 */
export function useApplyRuleFlow(onApplied?: () => void) {
  const { toast } = useToast();
  const previewRule = useRuleApplyPreview();
  const applyRule = useApplyRule();
  const undoApply = useUndoApply();
  const [pending, setPending] = React.useState<PendingApply | null>(null);

  function fail(title: string, err: unknown) {
    toast({
      title,
      description: err instanceof Error ? err.message : String(err),
      variant: "destructive",
    });
  }

  function start(rule: { id: number; pattern: string; kind?: string | null }) {
    previewRule.mutate(rule.id, {
      onSuccess: (preview) => {
        if (preview.matches === 0) {
          toast({ title: applyResultTitle(0), variant: "success" });
          onApplied?.();
          return;
        }
        setPending({ id: rule.id, pattern: rule.pattern, kind: rule.kind ?? null, preview });
      },
      onError: (err) => fail("Could not check the rule", err),
    });
  }

  function confirm() {
    if (!pending) return;
    applyRule.mutate(pending.id, {
      onSuccess: ({ updated, undo }) => {
        setPending(null);
        toast({
          title: applyResultTitle(updated),
          variant: "success",
          duration: 15_000,
          action:
            updated > 0
              ? {
                  label: "Undo",
                  onClick: () =>
                    undoApply.mutate(undo, {
                      onSuccess: ({ restored, skipped }) =>
                        toast({
                          title: `Undone — ${plural(restored, "transaction")} restored`,
                          description:
                            skipped.length > 0
                              ? `${plural(skipped.length, "transaction")} edited since, left as you set ${skipped.length === 1 ? "it" : "them"}.`
                              : undefined,
                        }),
                      onError: (err) => fail("Undo failed", err),
                    }),
                }
              : undefined,
        });
        onApplied?.();
      },
      onError: (err) => {
        setPending(null);
        fail("Could not apply the rule", err);
      },
    });
  }

  const dialog = pending ? (
    <ApplyConfirm
      pending={pending}
      busy={applyRule.isPending}
      onConfirm={confirm}
      onCancel={() => setPending(null)}
    />
  ) : null;

  return { start, dialog, busy: previewRule.isPending || applyRule.isPending };
}
