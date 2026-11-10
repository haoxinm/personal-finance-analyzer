/** A small flag on a grid row whose classification sources disagree; the edit dialog opened by
 * the row (or its category cell) has the one-click suggestions. */
export function ConflictMarker() {
  return (
    <span
      role="img"
      aria-label="Conflicting suggestions"
      title="Sources disagree about this category. Open the row to pick another with one click."
      className="shrink-0 rounded-full border border-amber-500/60 bg-amber-500/10 px-1.5 text-[10px] font-medium uppercase tracking-wide text-foreground"
    >
      conflict
    </span>
  );
}
