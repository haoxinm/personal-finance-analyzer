/** `YYYY-MM-DD` of today plus `days`, in local time. A date field's `max`. */
export function isoDateFromToday(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() + days);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** How far outside a statement's period a date may fall before the Add dialog warns. */
const PERIOD_SLACK_DAYS = 7;

function shiftIso(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

/** True when `date` is more than a week before `start` or after `end` (either may be unknown). */
export function outsideStatementPeriod(
  date: string,
  start: string | null | undefined,
  end: string | null | undefined,
): boolean {
  if (date === "") return false;
  if (start && date < shiftIso(start, -PERIOD_SLACK_DAYS)) return true;
  if (end && date > shiftIso(end, PERIOD_SLACK_DAYS)) return true;
  return false;
}
