import * as React from "react";

import type { components } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/use-toast";
import { EmptyState, ErrorState, LoadingState } from "@/components/primitives/states";

import { useCreateIssuer, useDeleteIssuer, useIssuers, usePreviewIssuerMatch, useUpdateIssuer } from "./hooks";

type Issuer = components["schemas"]["Issuer"];

const DEBOUNCE_MS = 300;

function parseTerms(text: string): string[] {
  return text
    .split(",")
    .map((t) => t.trim())
    .filter((t) => t.length > 0);
}

/**
 * Live "this would match these N past statements" check (§P3-F) for a set of `match_terms`,
 * debounced, via `POST /issuers/preview-match` (§3.12) — a real re-run of template matching
 * against each statement's actual text, not an approximation from today's account assignment.
 */
function useMatchPreviewCount(matchTerms: string[]): { count: number; loading: boolean } {
  const [debounced, setDebounced] = React.useState(matchTerms);

  React.useEffect(() => {
    const handle = setTimeout(() => setDebounced(matchTerms), DEBOUNCE_MS);
    return () => clearTimeout(handle);
    // `matchTerms` is a fresh array each render; compare by content, not identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [matchTerms.join("\u0000")]);

  const previewQuery = usePreviewIssuerMatch(debounced);
  return { count: previewQuery.data?.length ?? 0, loading: previewQuery.isFetching };
}

function IssuerRow({ issuer }: { issuer: Issuer }) {
  const [editing, setEditing] = React.useState(false);
  const [name, setName] = React.useState(issuer.name);
  const [termsText, setTermsText] = React.useState(issuer.match_terms.join(", "));
  const updateIssuer = useUpdateIssuer();
  const deleteIssuer = useDeleteIssuer();
  const { toast } = useToast();
  const liveTerms = editing ? parseTerms(termsText) : issuer.match_terms;
  const { count, loading: countLoading } = useMatchPreviewCount(liveTerms);

  function startEdit() {
    setName(issuer.name);
    setTermsText(issuer.match_terms.join(", "));
    setEditing(true);
  }

  function save() {
    if (name.trim().length === 0) return;
    updateIssuer.mutate(
      { id: issuer.id, body: { name: name.trim(), match_terms: parseTerms(termsText) } },
      {
        onSuccess: () => {
          toast({ title: "Issuer updated", variant: "success" });
          setEditing(false);
        },
        onError: (err) => {
          toast({
            title: "Could not update issuer",
            description: err instanceof Error ? err.message : String(err),
            variant: "destructive",
          });
        },
      },
    );
  }

  function remove() {
    if (
      !window.confirm(
        `Delete issuer "${issuer.name}"? Statements already attributed to it keep their data, but future imports will no longer auto-detect it.`,
      )
    ) {
      return;
    }
    deleteIssuer.mutate(issuer.id, {
      onError: (err) => {
        toast({
          title: "Could not delete issuer",
          description: err instanceof Error ? err.message : String(err),
          variant: "destructive",
        });
      },
    });
  }

  if (editing) {
    return (
      <div className="flex flex-col gap-2 border-b border-border py-3 last:border-b-0">
        <Input value={name} onChange={(e) => setName(e.target.value)} aria-label="Issuer name" />
        <Input
          value={termsText}
          onChange={(e) => setTermsText(e.target.value)}
          placeholder="Comma-separated match terms"
          aria-label="Match terms"
        />
        <span className="text-xs text-muted-foreground">
          {countLoading
            ? "Checking…"
            : `Would match ${count} past statement${count === 1 ? "" : "s"}.`}
        </span>
        <div className="flex gap-2">
          <Button size="sm" onClick={save} disabled={updateIssuer.isPending}>
            Save
          </Button>
          <Button size="sm" variant="outline" onClick={() => setEditing(false)}>
            Cancel
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-start justify-between gap-2 border-b border-border py-3 last:border-b-0">
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">{issuer.name}</span>
          <Badge variant="outline">{issuer.slug}</Badge>
        </div>
        <div className="flex flex-wrap gap-1">
          {issuer.match_terms.map((term) => (
            <Badge key={term} variant="secondary" className="font-mono text-xs">
              {term}
            </Badge>
          ))}
        </div>
        <span className="text-xs text-muted-foreground">
          {countLoading
            ? "Checking statements…"
            : `Matches ${count} past statement${count === 1 ? "" : "s"}.`}
        </span>
      </div>
      <div className="flex gap-2">
        <Button size="sm" variant="outline" onClick={startEdit}>
          Edit
        </Button>
        <Button size="sm" variant="destructive" onClick={remove} disabled={deleteIssuer.isPending}>
          Delete
        </Button>
      </div>
    </div>
  );
}

function IssuerCreateForm() {
  const [name, setName] = React.useState("");
  const [termsText, setTermsText] = React.useState("");
  const createIssuer = useCreateIssuer();
  const { toast } = useToast();

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (name.trim().length === 0) return;
    createIssuer.mutate(
      { name: name.trim(), match_terms: parseTerms(termsText) },
      {
        onSuccess: () => {
          toast({ title: "Issuer created", variant: "success" });
          setName("");
          setTermsText("");
        },
        onError: (err) => {
          toast({
            title: "Could not create issuer",
            description: err instanceof Error ? err.message : String(err),
            variant: "destructive",
          });
        },
      },
    );
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-2 sm:flex-row">
      <Input
        placeholder="Issuer name, e.g. Bank of America"
        value={name}
        onChange={(e) => setName(e.target.value)}
        aria-label="New issuer name"
      />
      <Input
        placeholder="Match terms (optional, comma-separated)"
        value={termsText}
        onChange={(e) => setTermsText(e.target.value)}
        aria-label="New issuer match terms"
      />
      <Button type="submit" disabled={createIssuer.isPending}>
        {createIssuer.isPending ? "Adding…" : "Add issuer"}
      </Button>
    </form>
  );
}

export function IssuerPanel() {
  const issuersQuery = useIssuers();

  return (
    <Card>
      <CardHeader>
        <CardTitle>Issuers ({issuersQuery.data?.length ?? 0})</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <IssuerCreateForm />
        {issuersQuery.isLoading ? <LoadingState rows={3} /> : null}
        {issuersQuery.isError ? (
          <ErrorState
            description={issuersQuery.error instanceof Error ? issuersQuery.error.message : undefined}
            onRetry={() => void issuersQuery.refetch()}
          />
        ) : null}
        {issuersQuery.data && issuersQuery.data.length === 0 ? (
          <EmptyState
            title="No issuers yet"
            description="Issuers are created from the Import screen, or here directly, and matched by editable terms (§2f.4)."
          />
        ) : null}
        {issuersQuery.data && issuersQuery.data.length > 0 ? (
          <div>
            {issuersQuery.data.map((issuer) => (
              <IssuerRow key={issuer.id} issuer={issuer} />
            ))}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
