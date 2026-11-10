import { Link } from "react-router-dom";

import type { components } from "@/api/client";
import { ErrorState, LoadingState } from "@/components/primitives/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

import { errorMessage } from "../logic";
import { Breakdown } from "./breakdown";
import { CrossCheckCard } from "./cross-check-card";
import { AllocationCard, EstimateCards } from "./estimate-cards";
import { estimateFailure, useTaxEstimate, useTaxProfile } from "./hooks";
import { filingStatusLabel, filingStatusSourceLabel, withUserNames, type TaxEstimate } from "./logic";
import { NotModeled, ParametersFootnote, WarningsList } from "./notes";
import { ProfileForm } from "./profile-form";

type User = components["schemas"]["User"];

export interface EstimateViewProps {
  userId: number;
  year: number;
  users: readonly User[];
  /** Switch the shell to another person (a conflict names the spouse to look at). */
  onSelectUser: (id: number) => void;
}

/**
 * One person's estimate for one year: the inputs panel beside the computed breakdown, with the
 * cross-check, warnings and parameter footnote. The inputs stay editable when the estimate itself
 * cannot be computed (no parameters for the year, spouses' profiles disagreeing), because that is
 * where a conflict is fixed.
 */
export function EstimateView({ userId, year, users, onSelectUser }: EstimateViewProps) {
  const estimateQuery = useTaxEstimate(userId, year);
  const profileQuery = useTaxProfile(userId, year);
  const estimate = estimateQuery.data;
  const profile = profileQuery.data ?? null;
  const nameOf = (id: number) => users.find((u) => u.id === id)?.name ?? `User ${id}`;

  const fallbackSpouse = estimate?.household_user_ids.find((id) => id !== userId) ?? null;
  const fallbackStatus = estimate?.filing_status ?? "single";
  // Remount the form when the stored profile (or, without one, the estimate's assumption) changes.
  const formKey = profile
    ? `saved-${profile.updated_at}`
    : `none-${fallbackStatus}-${fallbackSpouse ?? ""}`;

  const failure = estimateQuery.error ? estimateFailure(estimateQuery.error) : null;

  return (
    <div className="flex flex-col gap-4" data-testid="estimate-view">
      {estimate ? <WarningsList warnings={estimate.warnings} users={users} /> : null}
      {estimateQuery.isLoading ? (
        <LoadingState rows={2} />
      ) : estimateQuery.error ? (
        <EstimateProblem
          kind={failure ?? "other"}
          message={errorMessage(estimateQuery.error)}
          year={year}
          users={users}
          spouseId={profile?.joint_with_user_id ?? null}
          nameOf={nameOf}
          onSelectUser={onSelectUser}
          onRetry={() => void estimateQuery.refetch()}
        />
      ) : estimate ? (
        <>
          <Summary estimate={estimate} nameOf={nameOf} />
          <EstimateCards estimate={estimate} />
        </>
      ) : null}

      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        {profileQuery.isLoading ? (
          <LoadingState rows={4} />
        ) : profileQuery.error ? (
          <ErrorState
            description={errorMessage(profileQuery.error)}
            onRetry={() => void profileQuery.refetch()}
          />
        ) : (
          <ProfileForm
            key={formKey}
            userId={userId}
            year={year}
            users={users}
            profile={profile}
            fallback={{ filingStatus: fallbackStatus, spouseId: fallbackSpouse }}
          />
        )}
        {estimate ? (
          <div className="flex flex-col gap-4">
            {estimate.scope === "household_allocation" ? (
              <AllocationCard
                estimate={estimate}
                names={estimate.household_user_ids.map(nameOf).join(" and ")}
              />
            ) : null}
            <Breakdown calc={estimate.calc} household={estimate.scope === "household_allocation"} />
          </div>
        ) : null}
      </div>

      {estimate ? (
        <>
          <CrossCheckCard estimate={estimate} />
          <NotModeled items={estimate.not_modeled} />
          <ParametersFootnote parameters={estimate.parameters} />
        </>
      ) : null}
    </div>
  );
}

/** Filing status and scope, in a line above the numbers. */
function Summary({ estimate, nameOf }: { estimate: TaxEstimate; nameOf: (id: number) => string }) {
  const household = estimate.scope === "household_allocation";
  return (
    <p className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground" data-testid="estimate-summary">
      <Badge variant="secondary">{filingStatusLabel(estimate.filing_status)}</Badge>
      <span>{filingStatusSourceLabel(estimate.filing_status_source)}</span>
      {household ? (
        <>
          <span aria-hidden="true">·</span>
          <span>
            Joint return of {estimate.household_user_ids.map(nameOf).join(" and ")}: the household&apos;s tax is
            computed once and allocated by share of income.
          </span>
        </>
      ) : null}
    </p>
  );
}

function EstimateProblem({
  kind,
  message,
  year,
  users,
  spouseId,
  nameOf,
  onSelectUser,
  onRetry,
}: {
  kind: "no-parameters" | "spouse-conflict" | "other";
  message: string;
  year: number;
  users: readonly User[];
  spouseId: number | null;
  nameOf: (id: number) => string;
  onSelectUser: (id: number) => void;
  onRetry: () => void;
}) {
  if (kind === "no-parameters") {
    return (
      <div
        role="alert"
        data-testid="estimate-no-parameters"
        className="flex flex-col gap-2 rounded-lg border border-border bg-muted/30 p-6 text-sm"
      >
        <p className="font-medium">No tax parameters for {year}</p>
        <p className="text-muted-foreground">
          The estimate needs the IRS&apos;s figures for the year (brackets, standard deduction and limits), and
          this copy of the app does not have {year}. {message}
        </p>
        <p className="text-muted-foreground">
          Pick another year, or check for updated parameters in{" "}
          <Link to="/settings#tax-parameters" className="underline underline-offset-2">
            Settings › Tax parameters
          </Link>
          . You can also import your own parameter file with <code>pfa tax params import</code>.
        </p>
      </div>
    );
  }
  if (kind === "spouse-conflict") {
    return (
      <div
        role="alert"
        data-testid="estimate-spouse-conflict"
        className="flex flex-col gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-6 text-sm"
      >
        <p className="font-medium text-destructive">The spouses&apos; tax profiles for {year} disagree</p>
        <p className="text-muted-foreground">{withUserNames(message, users)}</p>
        <p className="text-muted-foreground">
          A joint return needs both people&apos;s profiles to be married filing jointly and to name each other.
          Fix it below: set this person&apos;s filing status or spouse, or remove the link, and save
          {spouseId !== null ? `; then check ${nameOf(spouseId)}'s profile too.` : "."}
        </p>
        {spouseId !== null ? (
          <div>
            <Button size="sm" variant="outline" onClick={() => onSelectUser(spouseId)}>
              Open {nameOf(spouseId)}&apos;s profile
            </Button>
          </div>
        ) : null}
      </div>
    );
  }
  return <ErrorState title="Could not compute the estimate" description={message} onRetry={onRetry} />;
}
