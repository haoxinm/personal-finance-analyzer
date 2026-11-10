import { Money } from "@/components/primitives/money";
import { Badge } from "@/components/ui/badge";

import {
  LINE_SOURCE_LABELS,
  resultKind,
  resultWord,
  type LineSource,
} from "./logic";

/** "Refund $1,234.00" / "Owe $1,234.00" for a signed result (payments minus tax). */
export function ResultAmount({ resultMinor }: { resultMinor: number }) {
  const kind = resultKind(resultMinor);
  return (
    <span data-testid="result-amount" data-kind={kind}>
      {resultWord(kind)}
      {kind === "even" ? null : (
        <>
          {" "}
          <Money tone="neutral" minorUnits={Math.abs(resultMinor)} />
        </>
      )}
    </span>
  );
}

/** A signed difference: "+$5.00" when positive, "-$5.00" when negative. */
export function SignedMoney({ minor }: { minor: number }) {
  return (
    <>
      {minor > 0 ? "+" : ""}
      <Money tone="neutral" minorUnits={minor} />
    </>
  );
}

const SOURCE_STYLE: Record<LineSource, string> = {
  documents: "border-sky-500/40 text-sky-700 dark:text-sky-300",
  profile: "border-violet-500/40 text-violet-700 dark:text-violet-300",
  computed: "text-muted-foreground",
};

/** Where a line of the computation comes from: imported documents, your profile inputs, or computed. */
export function SourceBadge({ source }: { source: LineSource }) {
  return (
    <Badge variant="outline" className={`px-2 py-0 text-xs ${SOURCE_STYLE[source]}`}>
      {LINE_SOURCE_LABELS[source]}
    </Badge>
  );
}
