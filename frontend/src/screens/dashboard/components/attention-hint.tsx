import * as React from "react";
import { Link } from "react-router-dom";

import type { ImportAttention } from "../hooks";

function plural(count: number, one: string, many: string): string {
  return `${count.toLocaleString()} ${count === 1 ? one : many}`;
}

/**
 * A quiet nudge on the Overview when something on the Import page needs a look: a statement whose
 * totals do not add up, one with no rows, or a file in the watched folder that could not be
 * imported. Each part links to the card that explains it. Hidden when everything is fine.
 */
const STORAGE_KEY = "attention-hint-hidden";

function readHidden(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function AttentionHint({
  attention,
}: {
  attention: Pick<ImportAttention, "statements" | "folderProblems"> & { signature?: string };
}) {
  const signature = attention.signature ?? `${attention.statements}|${attention.folderProblems}`;
  const [hidden, setHidden] = React.useState<string | null>(readHidden);
  if (attention.statements === 0 && attention.folderProblems === 0) return null;
  if (hidden === signature) return null;
  const hide = () => {
    setHidden(signature);
    try {
      window.localStorage.setItem(STORAGE_KEY, signature);
    } catch {
      /* storage unavailable: hidden for this visit only */
    }
  };
  return (
    <div
      role="status"
      data-testid="attention-hint"
      className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-amber-300 bg-amber-50 px-4 py-2.5 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200"
    >
      <span>Some imports need a look, so these totals may be off.</span>
      {attention.statements > 0 ? (
        <Link
          to="/spending/import#imported-statements"
          className="font-medium underline underline-offset-2 hover:opacity-80"
        >
          {plural(attention.statements, "statement needs", "statements need")} attention
        </Link>
      ) : null}
      {attention.folderProblems > 0 ? (
        <Link
          to="/spending/import#folder-imports"
          className="font-medium underline underline-offset-2 hover:opacity-80"
        >
          {plural(attention.folderProblems, "folder file", "folder files")} could not be imported
        </Link>
      ) : null}
      <button
        type="button"
        onClick={hide}
        className="ml-auto text-xs font-medium underline underline-offset-2 hover:opacity-80"
      >
        Hide
      </button>
    </div>
  );
}
