import { Navigate, useLocation } from "react-router-dom";

/**
 * `<Navigate>` to another path while keeping the current query string and hash, so a bookmarked
 * old URL's `?user=`, `?compare=`, `?statement_id=` etc. survive the redirect (a plain
 * `<Navigate to="/x">` drops them).
 */
export function RedirectKeepingSearch({ to }: { to: string }) {
  const { search, hash } = useLocation();
  return <Navigate to={{ pathname: to, search, hash }} replace />;
}
