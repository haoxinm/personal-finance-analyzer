/** Largest amount the dialog accepts, in minor units (the API allows 100_000_000_000). */
export const MAX_AMOUNT_MINOR = 100_000_000_000;

/**
 * Parses what the user typed into the amount box as a positive number of minor units (cents).
 * Accepts "12", "12.3", "12.30", "1,234.50" and ".5". Returns `null` for anything else: empty,
 * zero, negative, non-numeric, more than two decimals, or above 1,000,000,000.00. The sign
 * is never typed: the Money out / Money in control decides it (I5: positive = money out).
 */
export function parseAmountInput(text: string): number | null {
  const trimmed = text.trim();
  const match = /^(\d{1,3}(?:,\d{3})+|\d+)?(?:\.(\d{1,2}))?$/.exec(trimmed);
  if (!match) return null;
  const whole = match[1];
  const frac = match[2];
  if (whole === undefined && frac === undefined) return null;
  const major = whole === undefined ? 0 : Number(whole.replace(/,/g, ""));
  const minor = major * 100 + (frac === undefined ? 0 : Number(frac.padEnd(2, "0")));
  if (!Number.isSafeInteger(minor) || minor <= 0 || minor > 1_000_000_000 * 100) return null;
  return minor;
}

/** The magnitude of `minor` as a plain two-decimal string ("12.30"); the sign is dropped. */
export function formatAmountInput(minor: number): string {
  const abs = Math.abs(minor);
  const whole = Math.floor(abs / 100);
  const cents = String(abs % 100).padStart(2, "0");
  return `${whole}.${cents}`;
}
