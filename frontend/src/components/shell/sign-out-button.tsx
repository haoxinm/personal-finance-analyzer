import { authEnabled, signOut } from "@/api/auth";

/** "Sign out" for a server behind the login; renders nothing on a plain localhost install. */
export function SignOutButton() {
  if (!authEnabled()) return null;
  return (
    <button
      type="button"
      onClick={() => void signOut()}
      className="shrink-0 whitespace-nowrap rounded-md px-2 py-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
    >
      Sign out
    </button>
  );
}
