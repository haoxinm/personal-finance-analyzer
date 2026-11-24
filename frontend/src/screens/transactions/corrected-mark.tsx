/** A small pencil next to a value the user corrected after import. */
export function CorrectedMark() {
  return (
    <span title="Corrected by you" className="ml-1 text-muted-foreground">
      <svg
        aria-hidden="true"
        viewBox="0 0 16 16"
        className="inline-block h-3 w-3 align-baseline"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M11 2.5l2.5 2.5L5.5 13H3v-2.5L11 2.5z" />
      </svg>
      <span className="sr-only">corrected</span>
    </span>
  );
}
