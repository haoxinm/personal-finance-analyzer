import { Money } from "@/components/primitives/money";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/utils";

import { ResultAmount, SourceBadge } from "./display";
import { breakdownSections, type TaxEstimateCalc } from "./logic";

/**
 * The computation from income to the result, one table section at a time. Each input line says
 * whether it came from imported documents, from the profile inputs or was computed. For a joint
 * return this is the whole household's calculation (`scope` `household_allocation`).
 */
export function Breakdown({
  calc,
  household,
}: {
  calc: TaxEstimateCalc;
  household: boolean;
}) {
  const sections = breakdownSections(calc);
  return (
    <Card data-testid="estimate-breakdown">
      <CardHeader>
        <CardTitle>{household ? "Household calculation" : "How the estimate is computed"}</CardTitle>
        {household ? (
          <p className="text-xs text-muted-foreground">
            One joint return: both spouses&apos; forms and profile inputs, taxed once.
          </p>
        ) : null}
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <table className="w-full text-sm">
          <caption className="sr-only">Federal tax estimate from income to refund or amount owed</caption>
          {sections.map((section) => (
            <tbody key={section.key} data-testid={`estimate-section-${section.key}`}>
              <tr>
                <th
                  scope="colgroup"
                  colSpan={3}
                  className="pt-3 pb-1 text-left text-xs font-medium uppercase tracking-wide text-muted-foreground"
                >
                  {section.title}
                </th>
              </tr>
              {section.lines.map((line) => (
                <tr
                  key={line.key}
                  data-testid={`estimate-line-${line.key}`}
                  className={cn(
                    "border-t border-border/50",
                    line.kind === "subtotal" && "font-medium",
                    line.kind === "total" && "border-t-2 font-semibold",
                    line.kind === "memo" && "text-xs text-muted-foreground",
                  )}
                >
                  <td className={cn("py-1 pr-2", line.kind === "memo" && "pl-4 italic")}>{line.label}</td>
                  <td className="w-24 py-1 pr-2">{line.source ? <SourceBadge source={line.source} /> : null}</td>
                  <td className="w-32 py-1 text-right tabular-nums">
                    {line.minus ? <span aria-hidden="true">− </span> : null}
                    <Money tone="neutral" minorUnits={line.minor} />
                  </td>
                </tr>
              ))}
            </tbody>
          ))}
          <tbody>
            <tr className="border-t-2 border-border text-base font-semibold" data-testid="estimate-line-result">
              <td className="py-2 pr-2" colSpan={2}>
                {household ? "Household result" : "Result"}
                <span className="block text-xs font-normal text-muted-foreground">
                  Total payments minus total tax
                </span>
              </td>
              <td className="py-2 text-right tabular-nums">
                <ResultAmount resultMinor={calc.result_minor} />
              </td>
            </tr>
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}
