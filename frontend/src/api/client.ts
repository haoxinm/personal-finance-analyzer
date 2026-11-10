import createClient from "openapi-fetch";

import type { paths } from "./schema";

/**
 * The per-launch token the server injects into the served `index.html` (A31) as
 * `<meta name="pfa-token" content="...">`. Read once at module load; the meta tag does
 * not exist yet in Phase 1 (no server) or in Vite dev without a proxy target, so this is
 * `undefined` there and the header is simply omitted.
 */
function readPfaToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const meta = document.querySelector('meta[name="pfa-token"]');
  return meta?.getAttribute("content") ?? undefined;
}

/**
 * Same-origin `/api`, resolved to an absolute URL. `openapi-fetch` (like the WHATWG `URL`
 * constructor it uses internally) needs a base to resolve a path against; a bare `"/api"`
 * throws outside a full browser navigation context. The server always serves this same
 * origin (§4: FastAPI serves the built frontend as static files), so this is never a
 * cross-origin request.
 */
function resolveBaseUrl(): string {
  if (typeof window === "undefined") return "/api";
  return new URL("/api", window.location.origin).toString();
}

/**
 * The typed API client, generated from `openapi.json` (§3.12; see that file's `info.description`
 * for its provenance). Every request/response shape is inferred from the OpenAPI schema via
 * `openapi-fetch` — there is no hand-written per-endpoint client to keep in sync.
 *
 * Usage: `const { data, error } = await apiClient.GET("/transactions", { params: { query: {...} } })`.
 */
export const apiClient = createClient<paths>({
  baseUrl: resolveBaseUrl(),
  headers: (() => {
    const token = readPfaToken();
    return token ? { "X-PFA-Token": token } : {};
  })(),
});

export type { paths } from "./schema";
export type { components } from "./schema";

/**
 * Turns an `openapi-fetch` error payload (the `Error` schema's `{ detail }`, or `undefined`
 * on success) into a thrown `Error`, so TanStack Query hooks can `throwIfError(error)` instead
 * of `throw error` on a value that is not guaranteed to be an `Error` instance
 * (`only-throw-error`).
 */
export class ApiError extends Error {
  /** The raw `detail` from the error body (a string, or a structured object such as the issuer
   * 409's `{ message, existing_issuer_id }`). */
  readonly detail: unknown;

  constructor(message: string, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.detail = detail;
  }
}

/** The existing issuer's id from an issuer-conflict (409) error, if `error` is one. */
export function existingIssuerIdFromError(error: unknown): number | null {
  if (!(error instanceof ApiError)) return null;
  const detail = error.detail;
  if (typeof detail !== "object" || detail === null || !("existing_issuer_id" in detail)) return null;
  return typeof detail.existing_issuer_id === "number" ? detail.existing_issuer_id : null;
}

export function throwIfError(error: unknown): void {
  if (error === undefined || error === null) return;
  const detail =
    typeof error === "object" && "detail" in error ? error.detail : error;
  const message =
    typeof detail === "string"
      ? detail
      : typeof detail === "object" && detail !== null && "message" in detail && typeof detail.message === "string"
        ? detail.message
        : JSON.stringify(detail);
  throw new ApiError(message, detail);
}
