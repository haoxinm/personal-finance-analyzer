/**
 * Setting or clearing the LLM endpoint's API key.
 *
 * The key lives in the operating system's credential store (I7), is stored per host of the
 * endpoint's `base_url`, and is **write-only**: `GET /api/settings` never returns it (only
 * `has_key`), and this screen never displays a previously-saved key.
 *
 *   PUT    /api/settings/api-key            body {api_key, base_url} -> 204
 *   DELETE /api/settings/api-key?base_url=<base_url> -> 204
 */

function readPfaToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const meta = document.querySelector('meta[name="pfa-token"]');
  return meta?.getAttribute("content") ?? undefined;
}

function apiKeyUrl(query?: string): string {
  const origin = typeof window === "undefined" ? "http://localhost" : window.location.origin;
  const url = new URL("/api/settings/api-key", origin);
  if (query) url.search = query;
  return url.toString();
}

async function readErrorDetail(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "detail" in body) {
      return String(body.detail);
    }
  } catch {
    // fall through to the status-based message below
  }
  return `${response.status} ${response.statusText}`;
}

function jsonHeaders(): HeadersInit {
  const token = readPfaToken();
  return {
    "Content-Type": "application/json",
    ...(token ? { "X-PFA-Token": token } : {}),
  };
}

/** Writes a new API key for `baseUrl`'s host to the credential store. Never resolves with the key. */
export async function setApiKey(baseUrl: string, apiKey: string): Promise<void> {
  const response = await fetch(apiKeyUrl(), {
    method: "PUT",
    headers: jsonHeaders(),
    body: JSON.stringify({ api_key: apiKey, base_url: baseUrl }),
  });
  if (!response.ok) {
    throw new Error(await readErrorDetail(response));
  }
}

/** Removes the stored API key for `baseUrl`'s host from the credential store. */
export async function clearApiKey(baseUrl: string): Promise<void> {
  const response = await fetch(apiKeyUrl(`base_url=${encodeURIComponent(baseUrl)}`), {
    method: "DELETE",
    headers: jsonHeaders(),
  });
  if (!response.ok) {
    throw new Error(await readErrorDetail(response));
  }
}
