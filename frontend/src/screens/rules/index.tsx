import * as React from "react";
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

  const baseId = React.useId();
  const tabRefs = React.useRef<Array<HTMLButtonElement | null>>([]);
  const tabDomId = (id: TabId) => `${baseId}-tab-${id}`;
  const panelDomId = (id: TabId) => `${baseId}-panel-${id}`;

  function selectTab(tab: TabId) {
    const next = new URLSearchParams(searchParams);
    next.set("tab", tab);
    setSearchParams(next, { replace: true });
  }

  // Roving tabindex: only the active tab is a tab stop; arrows (and Home/End) move focus and
  // select, like the year picker.
  function onTabKeyDown(event: React.KeyboardEvent, index: number) {
    let target: number;
    if (event.key === "ArrowRight") target = (index + 1) % TABS.length;
    else if (event.key === "ArrowLeft") target = (index - 1 + TABS.length) % TABS.length;
    else if (event.key === "Home") target = 0;
    else if (event.key === "End") target = TABS.length - 1;
    else return;
    event.preventDefault();
    selectTab(TABS[target]!.id);
    tabRefs.current[target]?.focus();
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
        {TABS.map((tab, i) => (
          <Button
            key={tab.id}
            ref={(el) => {
              tabRefs.current[i] = el;
            }}
            id={tabDomId(tab.id)}
            role="tab"
            aria-selected={activeTab === tab.id}
            aria-controls={panelDomId(tab.id)}
            tabIndex={activeTab === tab.id ? 0 : -1}
            onKeyDown={(e) => onTabKeyDown(e, i)}
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

      {/* Every panel exists so each tab's `aria-controls` resolves; only the active one has content. */}
      {TABS.map((tab) => (
        <div
          key={tab.id}
          role="tabpanel"
          id={panelDomId(tab.id)}
          aria-labelledby={tabDomId(tab.id)}
          hidden={activeTab !== tab.id}
          tabIndex={0}
        >
          {tab.id === "rules" && activeTab === "rules" ? <RulesTab /> : null}
          {tab.id === "extractors" && activeTab === "extractors" ? <ExtractorsTab /> : null}
          {tab.id === "taxonomy" && activeTab === "taxonomy" ? <TaxonomyTab /> : null}
        </div>
      ))}
    </div>
  );
}

export const screenMeta = { path: "/spending/rules", title: "Rules" };
