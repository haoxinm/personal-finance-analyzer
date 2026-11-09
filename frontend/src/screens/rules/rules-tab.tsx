import * as React from "react";

import type { components } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useToast } from "@/components/ui/use-toast";
import { EmptyState, ErrorState, LoadingState } from "@/components/primitives/states";
import { cn } from "@/utils";

import {
  useCreateRule,
  useDeleteRule,
  useRules,
  useTaxonomy,
  useUpdateRule,
} from "./hooks";
import { useApplyRuleFlow } from "./use-apply-rule-flow";
import { MAX_PATTERN_LENGTH, type RuleMatchType, validatePattern } from "./rule-matching";
import { useRuleMatchPreview } from "./use-rule-match-preview";

type Rule = components["schemas"]["Rule"];
type Kind = components["schemas"]["Kind"];

const MATCH_TYPES: RuleMatchType[] = ["contains", "exact", "regex"];
const KINDS: Kind[] = ["purchase", "refund", "payment", "transfer", "fee", "interest", "adjustment"];

function MatchPreviewBadge({ pattern, matchType }: { pattern: string; matchType: RuleMatchType }) {
  const preview = useRuleMatchPreview(pattern, matchType, pattern.trim().length > 0);

  if (pattern.trim().length === 0) return null;
  if (preview.patternError) {
    return (
      <span className="text-xs text-destructive" role="alert">
        {preview.patternError}
      </span>
    );
  }
  if (preview.loading) {
    return <span className="text-xs text-muted-foreground">Checking matches…</span>;
  }
  return (
    <span className="text-xs text-muted-foreground" data-testid="match-preview">
      Matches {preview.matchCount} of {preview.searchedCount} transaction
      {preview.searchedCount === 1 ? "" : "s"} searched
      {preview.truncated ? " (most recent — some older transactions were not checked)" : ""}.
    </span>
  );
}

interface CategoryOption {
  categoryKey: string;
  categoryName: string;
  subcategoryKey: string;
  subcategoryName: string;
}

function useCategoryOptions(): CategoryOption[] {
  const { data } = useTaxonomy();
  return React.useMemo(() => {
    const options: CategoryOption[] = [];
    for (const category of data ?? []) {
      for (const sub of category.subcategories) {
        options.push({
          categoryKey: category.key,
          categoryName: category.name,
          subcategoryKey: sub.key,
          subcategoryName: sub.name,
        });
      }
    }
    return options;
  }, [data]);
}

interface RuleFormState {
  pattern: string;
  matchType: RuleMatchType;
  categorySubcategory: string; // "category_key::subcategory_key"
  kind: Kind | "";
}

const EMPTY_FORM: RuleFormState = { pattern: "", matchType: "contains", categorySubcategory: "", kind: "" };

function CategorySubcategorySelect({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (value: string) => void;
  options: CategoryOption[];
}) {
  return (
    <select
      className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label="Category / subcategory"
    >
      <option value="">Select category / subcategory…</option>
      {options.map((opt) => (
        <option
          key={`${opt.categoryKey}::${opt.subcategoryKey}`}
          value={`${opt.categoryKey}::${opt.subcategoryKey}`}
        >
          {opt.categoryName} / {opt.subcategoryName}
        </option>
      ))}
    </select>
  );
}

function RuleCreateForm({ options }: { options: CategoryOption[] }) {
  const [form, setForm] = React.useState<RuleFormState>(EMPTY_FORM);
  // A just-created rule that would change transactions already imported: offer to apply it.
  const [offer, setOffer] = React.useState<{
    id: number;
    pattern: string;
    kind: string | null;
    matches: number;
  } | null>(null);
  const createRule = useCreateRule();
  const { toast } = useToast();
  const applyFlow = useApplyRuleFlow(() => setOffer(null));

  const validation = validatePattern(form.pattern, form.matchType);
  const canSubmit = validation.valid && form.categorySubcategory !== "";

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    const [categoryKey, subcategoryKey] = form.categorySubcategory.split("::");
    if (!categoryKey || !subcategoryKey) return;
    createRule.mutate(
      {
        pattern: form.pattern.trim(),
        match_type: form.matchType,
        category_key: categoryKey,
        subcategory_key: subcategoryKey,
        kind: form.kind === "" ? null : form.kind,
      },
      {
        onSuccess: (rule) => {
          toast({ title: "Rule created", variant: "success" });
          setForm(EMPTY_FORM);
          const matches = rule?.existing_matches ?? 0;
          setOffer(
            rule && matches > 0
              ? { id: rule.id, pattern: rule.pattern, kind: rule.kind ?? null, matches }
              : null,
          );
        },
        onError: (err) => {
          toast({
            title: "Could not create rule",
            description: err instanceof Error ? err.message : String(err),
            variant: "destructive",
          });
        },
      },
    );
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-[2fr_1fr]">
        <div className="flex flex-col gap-1">
          <Input
            placeholder="Pattern, e.g. STARBUCKS"
            value={form.pattern}
            maxLength={MAX_PATTERN_LENGTH}
            onChange={(e) => setForm((f) => ({ ...f, pattern: e.target.value }))}
            aria-label="Pattern"
          />
          <MatchPreviewBadge pattern={form.pattern} matchType={form.matchType} />
        </div>
        <select
          className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm"
          value={form.matchType}
          onChange={(e) => setForm((f) => ({ ...f, matchType: e.target.value as RuleMatchType }))}
          aria-label="Match type"
        >
          {MATCH_TYPES.map((mt) => (
            <option key={mt} value={mt}>
              {mt}
            </option>
          ))}
        </select>
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-[2fr_1fr]">
        <CategorySubcategorySelect
          value={form.categorySubcategory}
          onChange={(v) => setForm((f) => ({ ...f, categorySubcategory: v }))}
          options={options}
        />
        <select
          className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm"
          value={form.kind}
          onChange={(e) => setForm((f) => ({ ...f, kind: e.target.value as Kind | "" }))}
          aria-label="Kind override (optional)"
        >
          <option value="">No kind override</option>
          {KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      </div>
      <div>
        <Button type="submit" disabled={!canSubmit || createRule.isPending}>
          {createRule.isPending ? "Adding…" : "Add rule"}
        </Button>
      </div>
      {offer ? (
        <div
          role="status"
          data-testid="apply-offer"
          className="flex flex-wrap items-center gap-3 rounded-md border border-border bg-muted/40 p-3 text-sm"
        >
          <span>
            &ldquo;{offer.pattern}&rdquo; also matches {offer.matches} transaction
            {offer.matches === 1 ? "" : "s"} you already have. Rules only apply to new imports unless
            you apply them. Transactions you categorized yourself are never changed.
          </span>
          <Button
            type="button"
            size="sm"
            disabled={applyFlow.busy}
            onClick={() => applyFlow.start(offer)}
          >
            {applyFlow.busy ? "Checking…" : `Apply to ${offer.matches} existing`}
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setOffer(null)}>
            Not now
          </Button>
        </div>
      ) : null}
      {applyFlow.dialog}
    </form>
  );
}

function RuleRow({ rule, options }: { rule: Rule; options: CategoryOption[] }) {
  const [editing, setEditing] = React.useState(false);
  const [pattern, setPattern] = React.useState(rule.pattern);
  const [categorySubcategory, setCategorySubcategory] = React.useState(
    `${rule.category_key}::${rule.subcategory_key}`,
  );
  const updateRule = useUpdateRule();
  const deleteRule = useDeleteRule();
  const applyFlow = useApplyRuleFlow();
  const { toast } = useToast();

  const editable = rule.source === "user";

  function startEdit() {
    setPattern(rule.pattern);
    setCategorySubcategory(`${rule.category_key}::${rule.subcategory_key}`);
    setEditing(true);
  }

  function save() {
    const [categoryKey, subcategoryKey] = categorySubcategory.split("::");
    if (!categoryKey || !subcategoryKey || pattern.trim().length === 0) return;
    updateRule.mutate(
      { id: rule.id, body: { pattern: pattern.trim(), category_key: categoryKey, subcategory_key: subcategoryKey } },
      {
        onSuccess: () => {
          toast({ title: "Rule updated", variant: "success" });
          setEditing(false);
        },
        onError: (err) => {
          toast({
            title: "Could not update rule",
            description: err instanceof Error ? err.message : String(err),
            variant: "destructive",
          });
        },
      },
    );
  }

  function remove() {
    if (!window.confirm(`Delete the rule "${rule.pattern}"?`)) return;
    deleteRule.mutate(rule.id, {
      onError: (err) => {
        toast({
          title: "Could not delete rule",
          description: err instanceof Error ? err.message : String(err),
          variant: "destructive",
        });
      },
    });
  }

  if (editing) {
    return (
      <div className="flex flex-col gap-2 border-b border-border py-3 last:border-b-0">
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-[2fr_2fr]">
          <Input value={pattern} onChange={(e) => setPattern(e.target.value)} aria-label="Edit pattern" />
          <CategorySubcategorySelect
            value={categorySubcategory}
            onChange={setCategorySubcategory}
            options={options}
          />
        </div>
        <div className="flex gap-2">
          <Button size="sm" onClick={save} disabled={updateRule.isPending}>
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
    <div className="flex flex-col gap-2 border-b border-border py-3 last:border-b-0">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm">{rule.pattern}</span>
          <Badge variant={rule.source === "user" ? "accent" : "secondary"}>{rule.source}</Badge>
          {rule.kind ? <Badge variant="outline">{rule.kind}</Badge> : null}
        </div>
        <span className="text-xs text-muted-foreground">
          → {rule.category_key} / {rule.subcategory_key}
        </span>
        <span className="text-xs text-muted-foreground" data-testid="rule-precedence">
          {rule.match_type} match · priority {rule.priority}
        </span>
      </div>
      {editable ? (
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => applyFlow.start(rule)}
            disabled={applyFlow.busy}
            title="Apply this rule to transactions you have already imported"
          >
            Apply to existing
          </Button>
          <Button size="sm" variant="outline" onClick={startEdit}>
            Edit
          </Button>
          <Button size="sm" variant="destructive" onClick={remove} disabled={deleteRule.isPending}>
            Delete
          </Button>
        </div>
      ) : (
        <span className="text-xs text-muted-foreground">Built-in rules are managed in the corpus, not here.</span>
      )}
    </div>
      {applyFlow.dialog}
    </div>
  );
}

const PAGE_SIZE = 25;

/** Rules whose pattern, category or subcategory matches `search` (case-insensitive substring on
 * any of the three) — with ~120 built-in rules (§ the corpus) plus whatever the user adds, a
 * search box is the fast way to find one without scrolling past a hundred you don't care about. */
function filterRules(rules: readonly Rule[], search: string): Rule[] {
  const needle = search.trim().toLowerCase();
  if (needle === "") return [...rules];
  return rules.filter(
    (rule) =>
      rule.pattern.toLowerCase().includes(needle) ||
      rule.category_key.toLowerCase().includes(needle) ||
      rule.subcategory_key.toLowerCase().includes(needle),
  );
}

export function RulesTab() {
  const rulesQuery = useRules();
  const options = useCategoryOptions();
  const [search, setSearch] = React.useState("");
  const [page, setPage] = React.useState(1);

  const allRules = React.useMemo(() => rulesQuery.data ?? [], [rulesQuery.data]);
  const filtered = React.useMemo(() => filterRules(allRules, search), [allRules, search]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const clampedPage = Math.min(page, pageCount);
  const start = (clampedPage - 1) * PAGE_SIZE;
  const pageRules = filtered.slice(start, start + PAGE_SIZE);

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>New rule</CardTitle>
        </CardHeader>
        <CardContent>
          <RuleCreateForm options={options} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Rules ({filtered.length}{filtered.length !== allRules.length ? ` of ${allRules.length}` : ""})</CardTitle>
          <p className="text-sm text-muted-foreground" data-testid="precedence-note">
            Order of precedence: one-time edits to a transaction &gt; your own rules (lower priority
            number wins; ties go to the older rule) &gt; built-in rules &gt; issuer category and merchant
            cache &gt; LLM. A rule applies to future imports; use &ldquo;Apply to existing&rdquo; to change
            transactions you already have. A transaction you edited by hand is never changed by a rule.
          </p>
        </CardHeader>
        <CardContent className={cn(rulesQuery.data && rulesQuery.data.length > 0 ? "" : undefined)}>
          {rulesQuery.isLoading ? <LoadingState rows={4} /> : null}
          {rulesQuery.isError ? (
            <ErrorState
              description={rulesQuery.error instanceof Error ? rulesQuery.error.message : undefined}
              onRetry={() => void rulesQuery.refetch()}
            />
          ) : null}
          {rulesQuery.data && rulesQuery.data.length === 0 ? (
            <EmptyState title="No rules yet" description="Add a rule above to auto-classify matching transactions." />
          ) : null}
          {rulesQuery.data && rulesQuery.data.length > 0 ? (
            <div className="flex flex-col gap-3">
              <Input
                placeholder="Search by pattern, category or subcategory…"
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value);
                  setPage(1);
                }}
                aria-label="Search rules"
              />
              {filtered.length === 0 ? (
                <p className="py-6 text-center text-sm text-muted-foreground">No rules match “{search}”.</p>
              ) : (
                <div>
                  {pageRules.map((rule) => (
                    <RuleRow key={rule.id} rule={rule} options={options} />
                  ))}
                </div>
              )}
              {pageCount > 1 ? (
                <div className="flex items-center justify-between gap-2 pt-1">
                  <span className="text-xs text-muted-foreground">
                    Showing {start + 1}–{Math.min(start + PAGE_SIZE, filtered.length)} of {filtered.length}
                  </span>
                  <div className="flex gap-2">
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={clampedPage <= 1}
                      onClick={() => setPage((p) => Math.max(1, p - 1))}
                    >
                      Previous
                    </Button>
                    <span className="self-center text-xs text-muted-foreground">
                      Page {clampedPage} of {pageCount}
                    </span>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={clampedPage >= pageCount}
                      onClick={() => setPage((p) => Math.min(pageCount, p + 1))}
                    >
                      Next
                    </Button>
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}
