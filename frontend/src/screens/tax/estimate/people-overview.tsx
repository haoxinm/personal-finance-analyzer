import type { components } from "@/api/client";
import { Money } from "@/components/primitives/money";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

import { errorMessage, formatRate } from "../logic";
import { ResultAmount } from "./display";
import { estimateFailure, useTaxEstimate } from "./hooks";
import { filingStatusLabel } from "./logic";

type User = components["schemas"]["User"];

/**
 * The "All users" view: one summary card per person for the year. The estimate is computed per
 * person (it needs that person's filing status, deduction and inputs), so the breakdown and the inputs
 * are one click away rather than merged into a household total that would hide a joint return's
 * allocation. A joint return's spouses each show their allocated share, never the sum twice.
 */
export function PeopleOverview({
  users,
  year,
  onSelectUser,
}: {
  users: readonly User[];
  year: number;
  onSelectUser: (id: number) => void;
}) {
  return (
    <div className="flex flex-col gap-3" data-testid="estimate-people">
      <p className="text-sm text-muted-foreground">
        The federal estimate is computed for one person at a time, because it depends on each person&apos;s
        filing status and inputs. Open a person to see the breakdown and edit their inputs. Spouses on a joint
        return each show their allocated share of the household tax, not the household total.
      </p>
      <div className="grid gap-3 md:grid-cols-2">
        {users.map((u) => (
          <PersonCard key={u.id} user={u} year={year} onSelect={() => onSelectUser(u.id)} />
        ))}
      </div>
    </div>
  );
}

function PersonCard({ user, year, onSelect }: { user: User; year: number; onSelect: () => void }) {
  const query = useTaxEstimate(user.id, year);
  const estimate = query.data;
  const household = estimate?.scope === "household_allocation";
  return (
    <Card data-testid={`estimate-person-${user.id}`}>
      <CardContent className="flex flex-col gap-2 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{user.name}</span>
          {estimate ? (
            <Badge variant="secondary">{filingStatusLabel(estimate.filing_status)}</Badge>
          ) : null}
          {household ? <Badge variant="outline">Joint: allocated share</Badge> : null}
        </div>
        {query.isLoading ? (
          <Skeleton className="h-10 w-full" />
        ) : query.error ? (
          <p className="text-sm text-muted-foreground" role="status">
            {estimateFailure(query.error) === "no-parameters"
              ? `No tax parameters for ${year}.`
              : estimateFailure(query.error) === "spouse-conflict"
                ? "The spouses' profiles disagree; open this person to fix it."
                : errorMessage(query.error)}
            {estimateFailure(query.error) === "no-parameters" || estimateFailure(query.error) === "spouse-conflict" ? null : (
              <>
                {" "}
                <Button size="sm" variant="outline" onClick={() => void query.refetch()}>
                  Try again
                </Button>
              </>
            )}
          </p>
        ) : estimate ? (
          <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1 text-sm">
            <dt className="text-muted-foreground">{household ? "Allocated tax" : "Total tax"}</dt>
            <dd className="text-right tabular-nums">
              <Money tone="neutral" minorUnits={estimate.user.total_tax_minor} />
            </dd>
            <dt className="text-muted-foreground">{household ? "Household effective rate" : "Effective rate"}</dt>
            <dd className="text-right tabular-nums">{formatRate(estimate.calc.effective_rate)}</dd>
            <dt className="font-medium">Result</dt>
            <dd className="text-right font-medium tabular-nums">
              <ResultAmount resultMinor={estimate.user.result_minor} />
            </dd>
          </dl>
        ) : null}
        <div>
          <Button size="sm" variant="outline" onClick={onSelect} aria-label={`View ${user.name}'s estimate`}>
            View estimate
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
