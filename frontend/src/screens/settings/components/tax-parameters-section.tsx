import * as React from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

import {
  type TaxParametersRefresh,
  type TaxSettings,
  type Settings,
  useRefreshTaxParameters,
  useTaxParametersQuery,
} from "../lib/hooks";
import {
  checkOutcomeLabel,
  coveredYearsLabel,
  lastCheckedLabel,
  parameterSourceLabel,
  versionsLabel,
} from "../lib/tax-params";

export interface TaxParametersSectionProps {
  settings: Settings;
  onSave: (tax: TaxSettings) => void;
  saving: boolean;
}

const OUTCOME_LABELS: Record<TaxParametersRefresh["outcome"], string> = {
  updated: "Updated",
  current: "Up to date",
  rejected: "Rejected",
  unreachable: "Could not reach GitHub",
};

/** Tax parameters: the opt-in yearly check for updated IRS figures (off by default), the parameter
 * file now in effect, and a "Check now" button (an explicit request, so it works with the toggle off). */
export function TaxParametersSection({ settings, onSave, saving }: TaxParametersSectionProps) {
  const saved = settings.tax?.auto_update_params ?? false;
  const [auto, setAuto] = React.useState(saved);
  React.useEffect(() => {
    setAuto(saved);
  }, [saved]);

  const parameters = useTaxParametersQuery();
  const refresh = useRefreshTaxParameters();
  const data = parameters.data;

  return (
    <Card data-testid="tax-parameters-section">
      <CardHeader>
        <CardTitle>Tax parameters</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <p className="text-xs text-muted-foreground">
          The tax estimate uses the IRS&apos;s yearly figures (brackets, standard deduction, limits). They ship
          inside the app, so the estimate works offline.
        </p>

        <div className="flex flex-col gap-2">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
            Check for updated IRS parameters once a year
          </label>
          <p className="text-xs text-muted-foreground">
            Off by default. When on, the first tax estimate or summary you open in a calendar year checks this
            project&apos;s GitHub repository for tax years the app does not have yet (it never replaces a year
            the app already has), at most once per calendar year. The
            request sends no personal data (no tax figures, no version, no cookies); GitHub sees your IP address
            like for any download. A network error is ignored. With it off, the app never contacts anyone for
            this.
          </p>
          <div>
            <Button onClick={() => onSave({ ...settings.tax, auto_update_params: auto })} disabled={auto === saved || saving}>
              Save tax settings
            </Button>
          </div>
        </div>

        <div className="flex flex-col gap-2 border-t pt-4">
          <span className="text-sm font-medium">Parameters in effect</span>
          {parameters.isPending ? (
            <p className="text-xs text-muted-foreground">Loading…</p>
          ) : parameters.isError || !data ? (
            <p role="alert" className="text-sm text-destructive">
              Could not load the tax parameter status: {String(parameters.error)}
            </p>
          ) : (
            <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm" data-testid="tax-parameters-status">
              <dt className="text-muted-foreground">Source</dt>
              <dd>{parameterSourceLabel(data.source)}</dd>
              <dt className="text-muted-foreground">Version</dt>
              <dd>
                {data.params_version}{" "}
                <span className="text-xs text-muted-foreground">({versionsLabel(data)})</span>
              </dd>
              <dt className="text-muted-foreground">Last checked</dt>
              <dd>{lastCheckedLabel(data.last_checked)}</dd>
              <dt className="text-muted-foreground">Last check outcome</dt>
              <dd>{checkOutcomeLabel(data.last_check_outcome)}</dd>
              <dt className="text-muted-foreground">Tax years covered</dt>
              <dd>{coveredYearsLabel(data.years.map((y) => y.tax_year))}</dd>
            </dl>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="secondary" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
              {refresh.isPending ? "Checking…" : "Check now"}
            </Button>
            <span className="text-xs text-muted-foreground">
              Makes one plain request to GitHub right now, even with the yearly check off.
            </span>
          </div>
          {refresh.isError ? (
            <p role="alert" className="text-sm text-destructive">
              Could not check for updates: {String(refresh.error)}
            </p>
          ) : refresh.data ? (
            <p
              role="status"
              data-testid="tax-parameters-refresh-result"
              className={refresh.data.outcome === "updated" || refresh.data.outcome === "current" ? "text-sm" : "text-sm text-destructive"}
            >
              <span className="font-medium">{OUTCOME_LABELS[refresh.data.outcome]}.</span> {refresh.data.detail}
            </p>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}
