/** A small person mark next to a row the user added by hand (not read from a statement). */
export function ManualMark() {
  return (
    <span role="img" title="Added by you" aria-label="Added by you" className="ml-1 text-foreground/70">
      <svg
        aria-hidden="true"
        viewBox="0 0 16 16"
        className="inline-block h-4 w-4 align-text-bottom"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <circle cx="8" cy="5.5" r="2.5" />
        <path d="M3 13.5c.6-2.4 2.6-3.5 5-3.5s4.4 1.1 5 3.5" />
      </svg>
      <span className="sr-only">added by you</span>
    </span>
  );
}
