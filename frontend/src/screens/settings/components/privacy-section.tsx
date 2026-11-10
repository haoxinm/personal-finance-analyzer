import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

import { type PrivacySettings, type Settings } from "../lib/hooks";
import { MIN_PII_ENTRY_LENGTH, entryKindLabel } from "../lib/pii";
import { PiiTryIt } from "./pii-try-it";

export interface PrivacySectionProps {
  settings: Settings;
  onSave: (privacy: PrivacySettings) => void;
  saving: boolean;
}

/** Privacy toggles (P3-E section): whether a copy of each imported PDF is kept, whether the
 * debug-only extract cache is written, and the personal-info entries removed before anything reaches an LLM (`pii_terms`; kind is
 * auto-detected server-side, shown here as a badge). */
export function PrivacySection({ settings, onSave, saving }: PrivacySectionProps) {
  const [draft, setDraft] = React.useState<PrivacySettings>(settings.privacy);
  const [newTerm, setNewTerm] = React.useState("");

  React.useEffect(() => {
    setDraft(settings.privacy);
  }, [settings.privacy]);

  const dirty = JSON.stringify(draft) !== JSON.stringify(settings.privacy);

  const addTerm = () => {
    const term = newTerm.trim();
    if (term.length < MIN_PII_ENTRY_LENGTH) return;
    if (draft.pii_terms.includes(term)) {
      setNewTerm("");
      return;
    }
    setDraft((prev) => ({ ...prev, pii_terms: [...prev.pii_terms, term] }));
    setNewTerm("");
  };

  const removeTerm = (term: string) => {
    setDraft((prev) => ({ ...prev, pii_terms: prev.pii_terms.filter((t) => t !== term) }));
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Privacy</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={draft.store_pdf_copies}
            onChange={(e) => setDraft((prev) => ({ ...prev, store_pdf_copies: e.target.checked }))}
          />
          Keep a copy of each imported statement PDF
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={draft.store_extract_cache}
            onChange={(e) => setDraft((prev) => ({ ...prev, store_extract_cache: e.target.checked }))}
          />
          Cache extracted page text for debugging (off by default)
        </label>

        <div className="flex flex-col gap-2">
          <span className="text-sm font-medium">Personal info to remove before sending to an LLM</span>
          <p className="text-xs text-muted-foreground">
            Add your name, family members&apos; names, phone numbers, address and account or SSN-like
            numbers. Names match ignoring case, spacing and &quot;Last, First&quot; order. Phone and account
            numbers match in any format, e.g. (312) 555-0100 = 312.555.0100 = 3125550100. Every match is
            removed from statement text, descriptions and (when the tax LLM fallback is on) tax-form text before anything is classified or sent to an LLM.
          </p>
          <div className="flex flex-wrap gap-2">
            {draft.pii_terms.map((term) => (
              <Badge key={term} variant="secondary" className="gap-1">
                {term}
                <span
                  data-testid="pii-kind"
                  className="rounded bg-background/60 px-1 text-xs uppercase tracking-wide text-muted-foreground"
                >
                  {entryKindLabel(term)}
                </span>
                <button
                  type="button"
                  aria-label={`Remove ${term}`}
                  className="ml-1 text-muted-foreground hover:text-foreground"
                  onClick={() => removeTerm(term)}
                >
                  ×
                </button>
              </Badge>
            ))}
          </div>
          <div className="flex items-end gap-2">
            <Input
              value={newTerm}
              onChange={(e) => setNewTerm(e.target.value)}
              placeholder="e.g. a name or (312) 555-0100"
              className="w-48"
              aria-label="New personal info entry"
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  addTerm();
                }
              }}
            />
            <Button size="sm" variant="secondary" onClick={addTerm} disabled={newTerm.trim().length < MIN_PII_ENTRY_LENGTH}>
              Add entry
            </Button>
          </div>
          <PiiTryIt terms={draft.pii_terms} />
        </div>

        <div>
          <Button onClick={() => onSave(draft)} disabled={!dirty || saving}>
            Save privacy settings
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
