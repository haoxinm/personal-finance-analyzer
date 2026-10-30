import { Link } from "react-router-dom";

import type { components } from "@/api/client";
import { ErrorState, LoadingState } from "@/components/primitives/states";

import { useAllStatements } from "./api";

type Statement = components["schemas"]["Statement"];

/** The statuses that actually need a hand-built layout spec (§2d.2's whole reason to be here) —
 * shown first and called out, so the person doesn't have to guess which of possibly many
 * statements this screen is for. */
const NEEDS_MAPPING: ReadonlySet<Statement["status"]> = new Set([
  "unsupported_layout",
  "awaiting_extractor",
]);

function statusLabel(status: Statement["status"]): string {
  switch (status) {
    case "unsupported_layout":
      return "Unsupported layout";
    case "awaiting_extractor":
      return "Awaiting extractor";
    case "no_text_layer":
      return "No text layer";
    case "error":
      return "Error";
    case "parsed":
      return "Parsed";
    case "pending":
      return "Pending";
    default:
      return status;
  }
}

/**
 * Shown when the Layout mapper is opened with no `?statement_id=` — a picker of the user's
 * statements, `unsupported_layout` and `awaiting_extractor` ones (the two statuses that actually
 * need a hand-built spec, §2d.2) called out first, rather than an instruction to hand-edit the
 * URL. Every row still links to this same screen with that id, so picking one is just a normal
 * navigation, not a special case.
 */
export function StatementPicker() {
  const statementsQuery = useAllStatements();

  if (statementsQuery.isLoading) return <LoadingState />;
  if (statementsQuery.isError) {
    return (
      <ErrorState
        description={
          statementsQuery.error instanceof Error ? statementsQuery.error.message : undefined
        }
        onRetry={() => void statementsQuery.refetch()}
      />
    );
  }

  const statements = statementsQuery.data ?? [];
  if (statements.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-border p-6 text-center text-sm text-muted-foreground">
        No statements yet — import one first.
      </p>
    );
  }

  const sorted = [...statements].sort((a, b) => {
    const aNeeds = NEEDS_MAPPING.has(a.status);
    const bNeeds = NEEDS_MAPPING.has(b.status);
    if (aNeeds !== bNeeds) return aNeeds ? -1 : 1;
    return b.id - a.id;
  });

  return (
    <div className="flex flex-col gap-2" data-testid="statement-picker">
      <p className="text-sm text-muted-foreground">
        Choose a statement to map. Ones that still need a layout are listed first.
      </p>
      <ul className="flex flex-col gap-1.5">
        {sorted.map((statement) => (
          <li key={statement.id}>
            <Link
              to={`/spending/layout-mapper?statement_id=${statement.id}`}
              className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2 text-sm hover:bg-muted/50"
            >
              <span>
                Statement #{statement.id}
                {statement.period_start ? ` — ${statement.period_start}` : ""}
              </span>
              <span
                className={
                  NEEDS_MAPPING.has(statement.status)
                    ? "rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-900 dark:bg-amber-950 dark:text-amber-200"
                    : "text-xs text-muted-foreground"
                }
              >
                {statusLabel(statement.status)}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
