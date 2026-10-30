import { NavLink, useLocation } from "react-router-dom";

import { carriedSearch, visibleTabs, type SectionSpec } from "@/navigation";
import { cn } from "@/utils";

export interface SectionTabsProps {
  section: SectionSpec;
}

/**
 * The active section's sub-tabs: the second row of the header (sticky from `md` up). Rendered by the shell (not an
 * `<Outlet>` layout) because compare mode renders the routed screen twice and in-route chrome
 * would duplicate. Renders nothing for a section with a single tab, and a hidden tab's route
 * (the Layout mapper) simply leaves every tab inactive.
 */
export function SectionTabs({ section }: SectionTabsProps) {
  const { search } = useLocation();
  const tabs = visibleTabs(section);
  if (tabs.length < 2) return null;
  return (
    <div className="border-b border-border">
      <nav aria-label={`${section.label} sections`} className="mx-auto flex max-w-screen-2xl flex-nowrap items-center gap-1 overflow-x-auto px-4 py-1.5">
        {tabs.map((tab) => (
          <NavLink
            key={tab.path}
            to={{ pathname: tab.path, search: carriedSearch(tab, search, true) }}
            end={tab.end}
            className={({ isActive }) =>
              cn(
                "shrink-0 whitespace-nowrap rounded-md px-2 py-1 text-sm text-muted-foreground transition-colors hover:text-foreground",
                isActive && "bg-muted text-foreground",
              )
            }
          >
            {tab.label}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
