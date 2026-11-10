/**
 * Sign-in state for a server that sits behind the login (`api/auth.py`, used when the app runs in
 * a container or otherwise listens beyond localhost). The login itself is a page the server
 * renders at `/login`; the SPA only needs to know whether a login exists (the server adds
 * `<meta name="pfa-auth" content="1">` to the page it serves to a signed-in browser), to send
 * the browser back to it when a session ends, and to sign out. On a plain localhost install there
 * is no such tag and nothing here does anything.
 */

function metaContent(name: string): string | undefined {
  if (typeof document === "undefined") return undefined;
  return document.querySelector(`meta[name="${name}"]`)?.getAttribute("content") ?? undefined;
}

/** Whether this server asks for a password. */
export function authEnabled(): boolean {
  return metaContent("pfa-auth") === "1";
}

/** Send the browser to the login page, which returns to the current page afterwards. */
export function redirectToLogin(): void {
  const { pathname, search } = window.location;
  window.location.assign(`/login?next=${encodeURIComponent(pathname + search)}`);
}

/** End the session on the server, then show the login page. Always lands on the login page, even
 * if the request fails (an expired session has nothing left to end). */
export async function signOut(): Promise<void> {
  const token = metaContent("pfa-token");
  try {
    await fetch("/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: token ? { "X-PFA-Token": token } : {},
    });
  } finally {
    window.location.assign("/login");
  }
}
