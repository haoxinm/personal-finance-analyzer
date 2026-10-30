import { useSearchParams } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { cn } from "@/utils";

import { ExtractorsTab } from "./extractors-tab";
import { RulesTab } from "./rules-tab";
import { TaxonomyTab } from "./taxonomy-tab";

const TABS = [
  { id: "rules", label: "Rules" },
  { id: "extractors", label: "Extractors" },
  { id: "taxonomy", label: "Taxonomy" },
] as const;

type TabId = (typeof TABS)[number]["id"];

function isTabId(value: string | null): value is TabId {
  return TABS.some((t) => t.id === value);
}

/**
 * Rules & Taxonomy screen (P3-F, plan §2f): rule CRUD with a live match preview, the Extractors
 * tab (layout specs + issuer CRUD), and a taxonomy browser with approve/merge actions.
 */
export default function RulesScreen() {
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab: TabId = isTabId(searchParams.get("tab")) ? (searchParams.get("tab") as TabId) : "rules";

  function selectTab(tab: TabId) {
    const next = new URLSearchParams(searchParams);
    next.set("tab", tab);
    setSearchParams(next, { replace: true });
  }

  return (
    <div className="flex flex-col gap-6 p-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Rules & Taxonomy</h1>
        <p className="text-sm text-muted-foreground">
          Manage classification rules, extractor specs and issuers, and the category taxonomy.
        </p>
      </div>

      <div role="tablist" aria-label="Rules & Taxonomy sections" className="flex gap-1 border-b border-border">
        {TABS.map((tab) => (
          <Button
            key={tab.id}
            role="tab"
            aria-selected={activeTab === tab.id}
            variant="ghost"
            className={cn(
              "rounded-none border-b-2 border-transparent px-3",
              activeTab === tab.id ? "border-primary font-semibold" : "text-muted-foreground",
            )}
            onClick={() => selectTab(tab.id)}
          >
            {tab.label}
          </Button>
        ))}
      </div>

      <div role="tabpanel">
        {activeTab === "rules" ? <RulesTab /> : null}
        {activeTab === "extractors" ? <ExtractorsTab /> : null}
        {activeTab === "taxonomy" ? <TaxonomyTab /> : null}
      </div>
    </div>
  );
}

export const screenMeta = { path: "/spending/rules", title: "Rules" };
