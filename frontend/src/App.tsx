import * as React from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";

import { Toaster } from "@/components/ui/toaster";
import { ToastContextProvider } from "@/components/ui/toast-provider";
import { ErrorBoundary } from "@/components/primitives/error-boundary";
import { cn } from "@/utils";
import { ComparePanes } from "@/components/shell/compare-panes";
import { RedirectKeepingSearch } from "@/components/shell/redirect-keeping-search";
import { SectionTabs } from "@/components/shell/section-tabs";
import { UserBar } from "@/components/shell/user-bar";
import { useCompareUser, useSelectedUser } from "@/hooks/use-selected-user";
import { APP_NAME, HOME_PATH, LEGACY_REDIRECTS, SECTIONS, carriedSearch, routeInfo } from "@/navigation";
import { ComponentGallery } from "@/routes/gallery";

import DashboardScreen, { screenMeta as dashboardMeta } from "@/screens/dashboard";
import ImportScreen, { screenMeta as importMeta } from "@/screens/import";
import ReviewScreen, { screenMeta as reviewMeta } from "@/screens/review";
import TransactionsScreen, { screenMeta as transactionsMeta } from "@/screens/transactions";
import TaxDocumentsScreen, { screenMeta as taxDocumentsMeta } from "@/screens/tax/documents-screen";
import TaxImportScreen, { screenMeta as taxImportMeta } from "@/screens/tax/import-screen";
import TaxOverviewScreen, { screenMeta as taxOverviewMeta } from "@/screens/tax/overview-screen";
import NetWorthScreen, { screenMeta as netWorthMeta } from "@/screens/net-worth";
import ForecastScreen, { screenMeta as forecastMeta } from "@/screens/net-worth/forecast";
import RulesScreen, { screenMeta as rulesMeta } from "@/screens/rules";
import SettingsScreen, { screenMeta as settingsMeta } from "@/screens/settings";
import LayoutMapperScreen, { screenMeta as layoutMapperMeta } from "@/screens/layout-mapper";

/**
 * Every screen and its route. `screenMeta.path` on each screen is the source of truth for its
 * route; what the shell does with a route (nav placement, user bar, compare, notes) comes from
 * `SECTIONS` in `navigation.ts`.
 */
const SCREENS = [
  { meta: dashboardMeta, Component: DashboardScreen },
  { meta: transactionsMeta, Component: TransactionsScreen },
  { meta: reviewMeta, Component: ReviewScreen },
  { meta: importMeta, Component: ImportScreen },
  { meta: rulesMeta, Component: RulesScreen },
  { meta: layoutMapperMeta, Component: LayoutMapperScreen },
  { meta: taxOverviewMeta, Component: TaxOverviewScreen },
  { meta: taxDocumentsMeta, Component: TaxDocumentsScreen },
  { meta: taxImportMeta, Component: TaxImportScreen },
  { meta: netWorthMeta, Component: NetWorthScreen },
  { meta: forecastMeta, Component: ForecastScreen },
  { meta: settingsMeta, Component: SettingsScreen },
] as const;

/**
 * The app shell: top nav (sections), the active section's sub-tabs, the user bar, providers, error
 * boundary and every route. The sub-tabs live here rather than in a layout route because compare
 * mode renders `routes` twice (once per pane), which would duplicate any chrome rendered inside a
 * route. Old flat paths (`/review`, `/import`, ...) redirect to their section, keeping the query.
 * `/gallery` (a developer tool, not a user-facing screen) is routable but left out of the nav.
 */
export function App() {
  return (
    <ToastContextProvider>
      <Shell />
      <Toaster />
    </ToastContextProvider>
  );
}

function Shell() {
  const { pathname, search } = useLocation();
  const sel = useSelectedUser();
  const compare = useCompareUser(sel.selectedUserId, sel.users, sel.isLoading);

  const info = routeInfo(pathname);
  const showBar = info.userBar;
  const compareSupported = info.compare;
  const comparing = showBar && compareSupported && compare.active;

  React.useEffect(() => {
    document.title = info.title;
  }, [info.title]);

  function changePrimary(id: number | null) {
    // Picking the user already on the right swaps the panes instead of showing one user twice.
    if (compare.active && id === compare.compareUserId) compare.setPair(id, sel.selectedUserId);
    else sel.setSelectedUserId(id);
  }
  function changeCompare(id: number | null) {
    if (id === sel.selectedUserId) compare.setPair(compare.compareUserId, id);
    else compare.setCompareUserId(id);
  }

  const routes = (
    <ErrorBoundary>
      <Routes>
        <Route path="/" element={<RedirectKeepingSearch to={HOME_PATH} />} />
        {Object.entries(LEGACY_REDIRECTS).map(([from, to]) => (
          <Route key={from} path={from} element={<RedirectKeepingSearch to={to} />} />
        ))}
        {SCREENS.map(({ meta, Component }) => (
          <Route key={meta.path} path={meta.path} element={<Component />} />
        ))}
        <Route path="/gallery" element={<ComponentGallery />} />
      </Routes>
    </ErrorBoundary>
  );

  const nameOf = (id: number | null) => sel.users.find((u) => u.id === id)?.name ?? "";

  return (
    <div className="flex min-h-screen flex-col">
      <header className="sticky top-0 z-30 bg-background">
        <div className="border-b border-border">
          <nav
            aria-label="Main"
            className="mx-auto flex max-w-screen-2xl flex-wrap items-center gap-4 px-4 py-3"
          >
            <span className="text-sm font-semibold tracking-tight">{APP_NAME}</span>
            {SECTIONS.filter((section) => !section.hidden).map((section) => {
              const first = section.tabs[0]!;
              return (
                <NavItem
                  key={section.key}
                  to={{
                    pathname: first.path,
                    // Keep an active compare across the screens that support it.
                    search: carriedSearch(first, search, false),
                  }}
                  active={info.section?.key === section.key}
                  className={section.pushRight ? "ml-auto" : undefined}
                >
                  {section.label}
                </NavItem>
              );
            })}
          </nav>
        </div>
        {info.section ? <SectionTabs section={info.section} /> : null}
        {showBar ? (
          <UserBar
            users={sel.users}
            isLoading={sel.isLoading}
            primaryId={sel.selectedUserId}
            onPrimaryChange={changePrimary}
            compareSupported={compareSupported}
            compareActive={comparing}
            compareUserId={compare.compareUserId}
            onCompareToggle={compare.setEnabled}
            onCompareUserChange={changeCompare}
            note={info.note}
          />
        ) : null}
      </header>
      <main
        className={cn(
          "mx-auto flex w-full flex-1 flex-col px-4 py-6",
          comparing ? "max-w-screen-2xl" : "max-w-6xl",
        )}
      >
        {comparing ? (
          <ComparePanes
            showShortcutBadge={info.tab?.path === reviewMeta.path}
            panes={[
              { side: "left", userId: sel.selectedUserId, name: nameOf(sel.selectedUserId), setUserId: changePrimary },
              { side: "right", userId: compare.compareUserId, name: nameOf(compare.compareUserId), setUserId: changeCompare },
            ]}
          >
            {() => routes}
          </ComparePanes>
        ) : (
          routes
        )}
      </main>
    </div>
  );
}

function NavItem({
  to,
  active,
  className,
  children,
}: {
  to: React.ComponentProps<typeof NavLink>["to"];
  active: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <NavLink
      to={to}
      // The section is active across all its tabs (including hidden ones), which NavLink's own
      // path matching cannot express.
      className={cn(
        "rounded-md px-2 py-1 text-sm text-muted-foreground transition-colors hover:text-foreground",
        active && "bg-muted text-foreground",
        className,
      )}
    >
      {children}
    </NavLink>
  );
}
