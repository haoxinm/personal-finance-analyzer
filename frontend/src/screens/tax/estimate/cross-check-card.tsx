import { Money } from "@/components/primitives/money";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

import { ResultAmount, SignedMoney } from "./display";
import { filingStatusLabel, type TaxEstimate } from "./logic";

/**
 * The filed Form 1040 beside the estimate, with the difference (estimate minus return). The return
 * is never an input of the estimate; a large difference usually means something the estimate does
 * not model, or a form that is missing. A joint return is compared with the household's totals.
 */
export function CrossCheckCard({ estimate }: { estimate: TaxEstimate }) {
  const check = estimate.cross_check;
  if (!check) return null;
  const { calc } = estimate;
  const rows: Array<{
    key: string;
    label: string;
    ret: number | null;
    est: number;
    delta: number | null;
    result?: boolean;
  }> = [
    {
      key: "tax",
      label: "Total tax",
      ret: check.return_total_tax_minor,
      est: calc.total_tax_minor,
      delta: check.delta_total_tax_minor,
    },
    {
      key: "withheld",
      label: "Federal tax withheld",
      ret: check.return_withheld_minor,
      est: calc.federal_withheld_minor,
      delta: check.delta_withheld_minor,
    },
    {
      key: "result",
      label: "Refund or amount owed",
      ret: check.return_result_minor,
      est: calc.result_minor,
      delta: check.delta_result_minor,
      result: true,
    },
  ];
  return (
    <Card data-testid="cross-check-card">
      <CardHeader>
        <CardTitle>Compared with your filed Form 1040</CardTitle>
        <p className="text-xs text-muted-foreground">
          {check.household
            ? "The joint return is compared with the household totals above. "
            : ""}
          The return is a cross-check only; it is never used in the estimate.
          {check.return_filing_status
            ? ` Filed as ${filingStatusLabel(check.return_filing_status).toLowerCase()}.`
            : ""}
        </p>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th scope="col" className="pb-1 font-medium">
                &nbsp;
              </th>
              <th scope="col" className="pb-1 text-right font-medium">
                Return
              </th>
              <th scope="col" className="pb-1 text-right font-medium">
                Estimate
              </th>
              <th scope="col" className="pb-1 text-right font-medium">
                Difference
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} className="border-t border-border/50" data-testid={`cross-check-${r.key}`}>
                <th scope="row" className="py-1 text-left font-normal">
                  {r.label}
                </th>
                <td className="py-1 text-right tabular-nums" data-testid={`cross-check-${r.key}-return`}>
                  {r.ret === null ? (
                    "—"
                  ) : r.result ? (
                    <ResultAmount resultMinor={r.ret} />
                  ) : (
                    <Money tone="neutral" minorUnits={r.ret} />
                  )}
                </td>
                <td className="py-1 text-right tabular-nums" data-testid={`cross-check-${r.key}-estimate`}>
                  {r.result ? (
                    <ResultAmount resultMinor={r.est} />
                  ) : (
                    <Money tone="neutral" minorUnits={r.est} />
                  )}
                </td>
                <td className="py-1 text-right tabular-nums" data-testid={`cross-check-${r.key}-delta`}>
                  {r.delta === null ? "—" : <SignedMoney minor={r.delta} />}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="text-xs text-muted-foreground">
          Difference = estimate minus return. A large difference usually means a credit, deduction or income
          the estimate does not model, or a form that is missing.
        </p>
      </CardContent>
    </Card>
  );
}
