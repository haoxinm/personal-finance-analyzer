import * as React from "react";

import { CategoryBadge } from "@/components/primitives/category-badge";
import { DateRangePicker, type DateRange } from "@/components/primitives/date-range-picker";
import { EmptyState, ErrorState, LoadingState } from "@/components/primitives/states";
import { Money } from "@/components/primitives/money";
import { Page, PageHeader } from "@/components/primitives/page";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/use-toast";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">{children}</CardContent>
    </Card>
  );
}

/**
 * A Storybook-less gallery: renders every shared primitive P1-F owns, so its behaviour and
 * visual identity can be reviewed without building a real screen (Accepts criteria).
 */
export function ComponentGallery() {
  const { toast } = useToast();
  const [range, setRange] = React.useState<DateRange>({ from: null, to: null });

  return (
    <Page>
      <PageHeader title="Component gallery" description="Every shared primitive P1-F owns, in one place." />

      <Section title="Money">
        <div className="flex flex-col gap-1 font-mono text-lg">
          <Money minorUnits={1234} />
          <Money minorUnits={-1234} />
          <Money minorUnits={0} />
          <Money minorUnits={-1234} showPlusForInflow />
          <Money minorUnits={1234} showGlyph />
          <Money minorUnits={-1234} showGlyph />
          <Money minorUnits={123456789} currency="JPY" />
        </div>
      </Section>

      <Section title="CategoryBadge">
        <div className="flex flex-wrap gap-2">
          <CategoryBadge categoryKey="groceries" />
          <CategoryBadge categoryKey="dining" subcategoryLabel="restaurants" />
          <CategoryBadge categoryKey="transport" needsReview />
          <CategoryBadge categoryKey="others" label="Others" />
        </div>
      </Section>

      <Section title="DateRangePicker">
        <DateRangePicker value={range} onChange={setRange} />
        <p className="text-xs text-muted-foreground">
          Selected: {range.from ?? "—"} to {range.to ?? "—"}
        </p>
      </Section>

      <Section title="Buttons">
        <div className="flex flex-wrap gap-2">
          <Button>Default</Button>
          <Button variant="secondary">Secondary</Button>
          <Button variant="outline">Outline</Button>
          <Button variant="ghost">Ghost</Button>
          <Button variant="destructive">Destructive</Button>
          <Button variant="link">Link</Button>
          <Button disabled>Disabled</Button>
        </div>
      </Section>

      <Section title="Badges">
        <div className="flex flex-wrap gap-2">
          <Badge>Default</Badge>
          <Badge variant="secondary">Secondary</Badge>
          <Badge variant="accent">Accent</Badge>
          <Badge variant="outline">Outline</Badge>
          <Badge variant="destructive">Destructive</Badge>
        </div>
      </Section>

      <Section title="Input">
        <Input placeholder="Search transactions…" className="max-w-xs" />
      </Section>

      <Section title="Toast">
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            onClick={() =>
              toast({ title: "Import complete", description: "42 transactions parsed." })
            }
          >
            Show default toast
          </Button>
          <Button
            variant="outline"
            onClick={() =>
              toast({
                title: "Classification failed",
                description: "The provider returned an error.",
                variant: "destructive",
              })
            }
          >
            Show error toast
          </Button>
        </div>
      </Section>

      <Section title="Loading / Empty / Error states">
        <div className="grid gap-4 sm:grid-cols-3">
          <div>
            <p className="mb-2 text-xs font-medium text-muted-foreground">Loading</p>
            <LoadingState rows={2} />
          </div>
          <div>
            <p className="mb-2 text-xs font-medium text-muted-foreground">Empty</p>
            <EmptyState title="No statements yet" description="Import a PDF to get started." />
          </div>
          <div>
            <p className="mb-2 text-xs font-medium text-muted-foreground">Error</p>
            <ErrorState description="The server could not be reached." onRetry={() => {}} />
          </div>
        </div>
      </Section>

      <Section title="Skeleton">
        <div className="flex flex-col gap-2">
          <Skeleton className="h-4 w-1/3" />
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-full" />
        </div>
      </Section>
    </Page>
  );
}
