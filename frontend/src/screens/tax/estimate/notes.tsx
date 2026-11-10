import { Link } from "react-router-dom";

import type { components } from "@/api/client";

import { withUserNames, type TaxEstimate } from "./logic";

type User = components["schemas"]["User"];

/** The server's warnings for this estimate (filing status assumed, missing forms, over-limit
 * deferrals, a return filed differently...), with user ids replaced by names. */
export function WarningsList({
  warnings,
  users,
}: {
  warnings: readonly string[];
  users: readonly User[];
}) {
  if (warnings.length === 0) return null;
  return (
    <div
      role="status"
      data-testid="estimate-warnings"
      className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm"
    >
      <p className="font-medium">Read before relying on this estimate</p>
      <ul className="mt-1 list-disc pl-5 text-muted-foreground">
        {warnings.map((w) => (
          <li key={w}>{withUserNames(w, users)}</li>
        ))}
      </ul>
    </div>
  );
}

/** What the estimate leaves out, collapsed by default (it is the same list for every result). */
export function NotModeled({ items }: { items: readonly string[] }) {
  if (items.length === 0) return null;
  return (
    <details
      data-testid="estimate-not-modeled"
      className="rounded-md border border-border p-3 text-sm"
    >
      <summary className="cursor-pointer font-medium">
        Not modeled ({items.length})
      </summary>
      <p className="mt-2 text-xs text-muted-foreground">
        The estimate does not include these. Where a figure matters, enter its total in your inputs.
      </p>
      <ul className="mt-1 list-disc pl-5 text-muted-foreground">
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </details>
  );
}

const SOURCE_TEXT: Record<TaxEstimate["parameters"]["source"], string> = {
  bundled: "bundled with the app",
  cached: "a downloaded update",
  override: "your own file",
};

/** Where the year's brackets and limits come from: the IRS revenue procedures, and which copy of
 * the parameter file is in effect. */
export function ParametersFootnote({ parameters }: { parameters: TaxEstimate["parameters"] }) {
  return (
    <p className="text-xs text-muted-foreground" data-testid="estimate-parameters">
      Tax year {parameters.tax_year} parameters (brackets, standard deduction, limits):{" "}
      {parameters.sources.length > 0 ? parameters.sources.join("; ") : "no citation recorded"}. Parameter
      data version {parameters.params_version}, source: {SOURCE_TEXT[parameters.source]}. See{" "}
      <Link to="/settings#tax-parameters" className="underline underline-offset-2">
        Settings › Tax parameters
      </Link>{" "}
      to check for an update. This is a planning aid, not tax advice or a substitute for your return.
    </p>
  );
}
