import * as React from "react";

import type { components } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useToast } from "@/components/ui/use-toast";

import { SelectField } from "../components/fields";
import { FILING_STATUS_OPTIONS, errorMessage } from "../logic";
import { useDeleteTaxProfile, useSaveTaxProfile } from "./hooks";
import {
  MONEY_FIELDS,
  ageLimits,
  clampAge,
  draftFromProfile,
  draftToBody,
  sameDraft,
  type DeductionMode,
  type DraftErrors,
  type EstimateFilingStatus,
  type ProfileDraft,
  type TaxProfile,
} from "./logic";
import { MoneyField } from "./money-field";

type User = components["schemas"]["User"];

const DEDUCTION_OPTIONS: Array<{ value: DeductionMode; label: string }> = [
  { value: "standard", label: "Standard deduction" },
  { value: "itemized", label: "Itemized deductions" },
];

/** 0 through `max`, as select options. */
function countOptions(max: number) {
  return Array.from({ length: max + 1 }, (_, n) => ({ value: String(n), label: String(n) }));
}

export interface ProfileFormProps {
  userId: number;
  year: number;
  users: readonly User[];
  /** The saved profile, `null` when there is none for this user and year. */
  profile: TaxProfile | null;
  /** What the estimate assumed without a profile: the form starts there. */
  fallback: { filingStatus: EstimateFilingStatus; spouseId: number | null };
}

/**
 * The manual inputs of the estimate for one user and year: filing status (and spouse), deduction,
 * and the dollar amounts no imported form covers. The parent remounts it (`key`) when the saved
 * profile changes, so the draft always starts from what is stored. Amounts are typed in dollars and
 * sent in cents; `PUT` replaces the whole profile.
 */
export function ProfileForm({ userId, year, users, profile, fallback }: ProfileFormProps) {
  const { toast } = useToast();
  const save = useSaveTaxProfile(userId, year);
  const remove = useDeleteTaxProfile(userId, year);
  const { filingStatus: fallbackStatus, spouseId: fallbackSpouse } = fallback;
  const baseline = React.useMemo(
    () => draftFromProfile(profile, { filingStatus: fallbackStatus, spouseId: fallbackSpouse }),
    [profile, fallbackStatus, fallbackSpouse],
  );
  const [draft, setDraft] = React.useState<ProfileDraft>(baseline);
  const [errors, setErrors] = React.useState<DraftErrors>({});
  const [saveError, setSaveError] = React.useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = React.useState(false);

  const dirty = !sameDraft(draft, baseline);
  const spouses = users.filter((u) => u.id !== userId);

  function patch(next: Partial<ProfileDraft>) {
    // The age counts have limits that depend on the filing status: keep them valid as it changes.
    setDraft((d) => clampAge({ ...d, ...next }));
    setErrors((e) => ({ ...e, age65OrBlind: undefined, age65: undefined }));
    setSaveError(null);
  }
  const limits = ageLimits(draft.filingStatus);
  function setMoney(key: (typeof MONEY_FIELDS)[number]["key"], value: string) {
    setDraft((d) => ({ ...d, money: { ...d.money, [key]: value } }));
    setErrors((e) => ({ ...e, [key]: undefined }));
    setSaveError(null);
  }

  async function onSave(event: React.FormEvent) {
    event.preventDefault();
    const result = draftToBody(draft);
    if ("errors" in result) {
      setErrors(result.errors);
      return;
    }
    setErrors({});
    try {
      await save.mutateAsync(result.body);
      toast({ variant: "success", title: `Tax profile for ${year} saved` });
    } catch (e) {
      setSaveError(errorMessage(e, "Could not save the profile."));
    }
  }

  async function onDelete() {
    try {
      await remove.mutateAsync();
      setConfirmingDelete(false);
      toast({ title: `Tax profile for ${year} deleted` });
    } catch (e) {
      setConfirmingDelete(false);
      setSaveError(errorMessage(e, "Could not delete the profile."));
    }
  }

  return (
    <Card data-testid="profile-form">
      <CardHeader>
        <CardTitle>Your inputs for {year}</CardTitle>
      </CardHeader>
      <CardContent>
        <form className="flex flex-col gap-4" onSubmit={(e) => void onSave(e)} noValidate>
          <p className="text-xs text-muted-foreground">
            Income from your imported W-2, 1099 and manual forms is picked up automatically. Enter only what
            they do not cover. W-2 box 1 already excludes 401(k) and HSA payroll deductions, so do not enter
            those again as adjustments.
          </p>

          <div className="grid gap-3 sm:grid-cols-2">
            <SelectField<EstimateFilingStatus>
              label="Filing status"
              value={draft.filingStatus}
              options={FILING_STATUS_OPTIONS}
              onChange={(v) => v && patch({ filingStatus: v })}
            />
            {draft.filingStatus === "mfj" ? (
              <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                Spouse
                <select
                  value={draft.spouseId ?? ""}
                  onChange={(e) =>
                    patch({ spouseId: e.target.value === "" ? null : Number(e.target.value) })
                  }
                  className="flex h-8 rounded-md border border-input bg-transparent px-2 py-1 text-sm text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <option value="">No spouse linked</option>
                  {spouses.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.name}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
          </div>
          {draft.filingStatus === "mfj" ? (
            <p className="-mt-2 text-xs text-muted-foreground">
              {spouses.length === 0
                ? "To link a spouse, add them as a user in Settings first. "
                : "Linking a spouse computes one joint return from both people's forms and profile inputs (they are added together); each person then sees their allocated share. "}
              Without a link, this is a joint return filed on this person&apos;s forms alone.
            </p>
          ) : null}

          <div className="grid gap-3 sm:grid-cols-2">
            <SelectField<DeductionMode>
              label="Deduction"
              value={draft.deductionMode}
              options={DEDUCTION_OPTIONS}
              onChange={(v) => v && patch({ deductionMode: v })}
            />
            <SelectField<string>
              label="Age 65 or older / blind boxes checked"
              value={String(draft.age65OrBlind)}
              options={countOptions(limits.boxes)}
              onChange={(v) => patch({ age65OrBlind: Number(v) })}
            />
            <SelectField<string>
              label="People aged 65 or older (senior deduction, 2025-2028)"
              value={String(draft.age65)}
              options={countOptions(Math.min(limits.age65, draft.age65OrBlind))}
              onChange={(v) => patch({ age65: Number(v) })}
            />
          </div>
          <p className="-mt-2 text-xs text-muted-foreground">
            Check a box for each person who is 65 or older or blind. Of those, count the people who are 65 or
            older again in the second field: only they qualify for the senior deduction. It is at most{" "}
            {limits.age65} for {draft.filingStatus === "mfj" ? "a joint return" : "this filing status"}.
          </p>
          {errors.age65OrBlind || errors.age65 ? (
            <p role="alert" className="text-xs text-destructive">
              {errors.age65OrBlind ?? errors.age65}
            </p>
          ) : null}
          {draft.deductionMode === "itemized" ? (
            <MoneyField
              label="Itemized deductions"
              help="Your total after limits such as the SALT cap and phase-outs; the estimate does not apply them."
              value={draft.itemized}
              onChange={(v) => {
                patch({ itemized: v });
                setErrors((e) => ({ ...e, itemized: undefined }));
              }}
              error={errors.itemized}
            />
          ) : null}

          {MONEY_FIELDS.map((f) => (
            <MoneyField
              key={f.key}
              label={f.label}
              help={f.help}
              value={draft.money[f.key]}
              onChange={(v) => setMoney(f.key, v)}
              error={errors[f.key]}
            />
          ))}

          {saveError ? (
            <p role="alert" className="text-sm text-destructive">
              {saveError}
            </p>
          ) : null}

          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" size="sm" disabled={save.isPending || (!dirty && profile !== null)}>
              {save.isPending ? "Saving…" : "Save profile"}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={!dirty}
              onClick={() => {
                setDraft(baseline);
                setErrors({});
                setSaveError(null);
              }}
            >
              Reset
            </Button>
            {profile !== null ? (
              confirmingDelete ? (
                <span className="flex flex-wrap items-center gap-2">
                  <span className="text-xs">Delete the saved profile for {year}?</span>
                  <Button
                    type="button"
                    size="sm"
                    variant="destructive"
                    disabled={remove.isPending}
                    onClick={() => void onDelete()}
                  >
                    Confirm delete
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => setConfirmingDelete(false)}
                  >
                    Keep
                  </Button>
                </span>
              ) : (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => setConfirmingDelete(true)}
                >
                  Delete profile
                </Button>
              )
            ) : (
              <span className="text-xs text-muted-foreground">
                Not saved yet: the estimate assumes the standard deduction and the filing status shown.
              </span>
            )}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
