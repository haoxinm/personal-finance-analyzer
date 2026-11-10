/**
 * Per-viewer conveniences for the Import screen, all `localStorage`-backed and all optional:
 * losing them just means one extra confirmation click. Never
 * used for anything the server needs to know (A22's per-issuer skip-confirmation lives only here; the server-side "remembered spec" is `issuers.default_spec_id`,
 * §2f.3, set via `ExtractRequest.remember`).
 *
 * Every accessor is wrapped in try/catch (private browsing, blocked storage, SSR) per the
 * artifact/browser-storage rule this app follows generally: storage failures never break render.
 */

const SKIP_CONFIRM_KEY = "pfa:import:skip-confirm-issuer-ids";

function readSkipConfirmIds(): number[] {
  try {
    const raw = window.localStorage.getItem(SKIP_CONFIRM_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((v): v is number => typeof v === "number");
  } catch {
    return [];
  }
}

/** Whether the "don't ask again" toggle is on for `issuerId` (A22: collapses the confirmation
 * step to zero clicks for a confident proposal from this issuer). */
export function isSkipConfirmForIssuer(issuerId: number): boolean {
  return readSkipConfirmIds().includes(issuerId);
}

export function setSkipConfirmForIssuer(issuerId: number, skip: boolean): void {
  try {
    const current = new Set(readSkipConfirmIds());
    if (skip) {
      current.add(issuerId);
    } else {
      current.delete(issuerId);
    }
    window.localStorage.setItem(SKIP_CONFIRM_KEY, JSON.stringify(Array.from(current)));
  } catch {
    // Ignore: this is a convenience, not a requirement.
  }
}
