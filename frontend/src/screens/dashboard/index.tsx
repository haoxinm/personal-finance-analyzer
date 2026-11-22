import * as React from "react";
import { Link } from "react-router-dom";

import type { DateRange } from "@/components/primitives/date-range-picker";
import { Page, PageHeader } from "@/components/primitives/page";
import { EmptyState, ErrorState, LoadingState } from "@/components/primitives/states";
import { Button } from "@/components/ui/button";
import { useTaxonomy } from "@/api/hooks";
import { ChartTypeToggle } from "@/components/primitives/chart-type-toggle";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { usePersistentChoice } from "@/hooks/use-persistent-choice";
import { useSelectedUser } from "@/hooks/use-selected-user";
import { useSpendQueryParams } from "@/hooks/use-spend-query-params";

import { colorMapForKeys } from "./chart-colors";
import { CategoryLines } from "./components/category-lines";
import { CategoryPie } from "./components/category-pie";
import { CategoryStackedBar } from "./components/category-stacked-bar";
import { CurrencyBanner } from "./components/currency-banner";
import { FiltersBar } from "./components/filters-bar";
import { MonthOverMonth } from "./components/month-over-month";
import { TopMerchants } from "./components/top-merchants";
import { TrendBars } from "./components/trend-bars";
import { TrendLine } from "./components/trend-line";
import { UserComparison } from "./components/user-comparison";
import {
  type DashboardFilters,
  useAccounts,
  useCategoryBreakdown,
  useTopMerchants,
  useTrend,
  useUserBreakdown,
} from "./hooks";
import {
  computeMonthOverMonthDelta,
  defaultCategoryLabel,
  detectOtherCurrencies,
  distinctCategories,
  pivotByCategory,
  toTrendPoints,
  totalsByUser,
} from "./logic";

const CATEGORY_CHART_TYPES = ["bar", "line", "pie"] as const;
const TREND_CHART_TYPES = ["line", "bar"] as const;

/**
 * The Spending Overview screen (P3-D): period selector, category composition over time, an overall
 * trend line, top merchants, the most recent period-over-period change, a per-user comparison
 * when more than one user exists, and the D5 mixed-currency banner. Every chart degrades to an
 * `EmptyState` before any data exists, rather than rendering an empty axis.
 */
export default function DashboardScreen() {
  const [params, setParams] = useSpendQueryParams();
  const sel = useSelectedUser();
  const accountsQuery = useAccounts();

  const granularity = params.granularity ?? "month";
  const currency = params.currency ?? "USD";

  const filters: DashboardFilters = React.useMemo(
    () => ({
      userIds: sel.userIds ?? params.userIds,
      dateFrom: params.dateFrom,
      dateTo: params.dateTo,
      currency,
    }),
    [sel.userIds, params.userIds, params.dateFrom, params.dateTo, currency],
  );

  const users = sel.users;
  // The by-user comparison only makes sense across everyone; one selected user is a single bar.
  const showUserComparison = users.length > 1 && sel.selectedUserId === null;
  const ready = !sel.isLoading;

  const categoryQuery = useCategoryBreakdown(filters, granularity, ready);
  const trendQuery = useTrend(filters, granularity, ready);
  const topMerchantsQuery = useTopMerchants(filters, 8, ready);
  const userBreakdownQuery = useUserBreakdown(filters, showUserComparison && ready);

  const dateRange: DateRange = { from: params.dateFrom ?? null, to: params.dateTo ?? null };

  const currencyOptions = React.useMemo(() => {
    const set = new Set<string>([currency]);
    for (const account of accountsQuery.data ?? []) set.add(account.currency);
    return Array.from(set).sort();
  }, [accountsQuery.data, currency]);

  const otherCurrencies = React.useMemo(
    () => detectOtherCurrencies(accountsQuery.data ?? [], currency),
    [accountsQuery.data, currency],
  );

  const trendPoints = React.useMemo(() => toTrendPoints(trendQuery.data ?? []), [trendQuery.data]);
  const monthOverMonth = React.useMemo(() => computeMonthOverMonthDelta(trendPoints), [trendPoints]);
  const userTotals = React.useMemo(() => totalsByUser(userBreakdownQuery.data ?? []), [userBreakdownQuery.data]);

  const [categoryChart, setCategoryChart] = usePersistentChoice<(typeof CATEGORY_CHART_TYPES)[number]>(
    "pfa:overview:category-chart",
    CATEGORY_CHART_TYPES,
    "bar",
  );
  const [trendChart, setTrendChart] = usePersistentChoice<(typeof TREND_CHART_TYPES)[number]>(
    "pfa:overview:trend-chart",
    TREND_CHART_TYPES,
    "line",
  );

  // One colour map and one label lookup for all three category charts, so a category keeps its
  // colour and its taxonomy name when the chart type changes.
  const taxonomyQuery = useTaxonomy();
  const categoryColors = React.useMemo(
    () => colorMapForKeys(distinctCategories(pivotByCategory(categoryQuery.data ?? [])), true),
    [categoryQuery.data],
  );
  const labelFor = React.useMemo(() => {
    const names = new Map<string, string>();
    if (Array.isArray(taxonomyQuery.data)) {
      for (const category of taxonomyQuery.data) names.set(category.key, category.name);
    }
    return (key: string) => names.get(key) ?? defaultCategoryLabel(key);
  }, [taxonomyQuery.data]);

  const pieCaption =
    params.dateFrom || params.dateTo
      ? `Share of spending, ${params.dateFrom ?? "start"} – ${params.dateTo ?? "today"}`
      : "Share of spending, all dates";

  const anyError = categoryQuery.error ?? trendQuery.error ?? topMerchantsQuery.error;
  const anyLoading =
    categoryQuery.isLoading || trendQuery.isLoading || topMerchantsQuery.isLoading || sel.isLoading;

  const hasAnyData =
    (categoryQuery.data?.length ?? 0) > 0 ||
    (trendQuery.data?.length ?? 0) > 0 ||
    (topMerchantsQuery.data?.length ?? 0) > 0;

  return (
    <Page>
      <PageHeader
        title="Spending overview"
        description="Spending by category over time, top merchants, and how this period compares to the last."
      />

      <FiltersBar
        dateRange={dateRange}
        onDateRangeChange={(range) => setParams({ dateFrom: range.from ?? undefined, dateTo: range.to ?? undefined })}
        granularity={granularity}
        onGranularityChange={(g) => setParams({ granularity: g })}
        currency={currency}
        currencyOptions={currencyOptions}
        onCurrencyChange={(c) => setParams({ currency: c })}
      />

      <CurrencyBanner currencies={otherCurrencies} selectedCurrency={currency} />

      {anyError ? (
        <ErrorState
          description={anyError instanceof Error ? anyError.message : String(anyError)}
          onRetry={() => {
            void categoryQuery.refetch();
            void trendQuery.refetch();
            void topMerchantsQuery.refetch();
          }}
        />
      ) : anyLoading ? (
        <LoadingState rows={4} />
      ) : !hasAnyData ? (
        <EmptyState
          title="No data yet"
          description="Import a statement and classify its transactions to see your spending overview."
          action={
            <Button asChild size="sm" variant="outline">
              <Link to="/spending/import">Import statements</Link>
            </Button>
          }
        />
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card className="lg:col-span-2">
            <CardHeader className="flex-row flex-wrap items-center justify-between gap-2 space-y-0">
              <div className="flex flex-col gap-1">
                <CardTitle>Spending by category</CardTitle>
                {categoryChart === "pie" ? <CardDescription>{pieCaption}</CardDescription> : null}
              </div>
              <ChartTypeToggle
                chartLabel="Spending by category"
                value={categoryChart}
                options={CATEGORY_CHART_TYPES}
                onChange={setCategoryChart}
              />
            </CardHeader>
            <CardContent>
              {categoryChart === "pie" ? (
                <CategoryPie
                  rows={categoryQuery.data ?? []}
                  currency={currency}
                  colors={categoryColors}
                  labelFor={labelFor}
                />
              ) : categoryChart === "line" ? (
                <CategoryLines
                  rows={categoryQuery.data ?? []}
                  currency={currency}
                  colors={categoryColors}
                  labelFor={labelFor}
                />
              ) : (
                <CategoryStackedBar
                  rows={categoryQuery.data ?? []}
                  currency={currency}
                  colors={categoryColors}
                  labelFor={labelFor}
                />
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex-row flex-wrap items-center justify-between gap-2 space-y-0">
              <CardTitle>Trend</CardTitle>
              <ChartTypeToggle
                chartLabel="Trend"
                value={trendChart}
                options={TREND_CHART_TYPES}
                onChange={setTrendChart}
              />
            </CardHeader>
            <CardContent>
              {trendChart === "bar" ? (
                <TrendBars rows={trendQuery.data ?? []} currency={currency} />
              ) : (
                <TrendLine rows={trendQuery.data ?? []} currency={currency} />
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>This period vs. last</CardTitle>
            </CardHeader>
            <CardContent>
              <MonthOverMonth delta={monthOverMonth} currency={currency} />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Top merchants</CardTitle>
            </CardHeader>
            <CardContent>
              <TopMerchants rows={topMerchantsQuery.data ?? []} currency={currency} />
            </CardContent>
          </Card>

          {showUserComparison ? (
            <Card>
              <CardHeader>
                <CardTitle>By user</CardTitle>
              </CardHeader>
              <CardContent>
                {userBreakdownQuery.isLoading ? (
                  <LoadingState rows={2} />
                ) : (
                  <UserComparison totals={userTotals} users={users} currency={currency} />
                )}
              </CardContent>
            </Card>
          ) : null}
        </div>
      )}
    </Page>
  );
}

export const screenMeta = { path: "/spending", title: "Overview" };
