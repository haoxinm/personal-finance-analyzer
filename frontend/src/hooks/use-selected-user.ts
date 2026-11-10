import * as React from "react";
import { useSearchParams } from "react-router-dom";

import { useUsers } from "@/api/hooks";
import type { components } from "@/api/client";

export type User = components["schemas"]["User"];

/** URL search param holding the explicit selection: `?user=<id>` or `?user=all`. */
export const SELECTED_USER_PARAM = "user";
/** localStorage key restoring the selection when navigating between screens (nav links carry no query). */
export const SELECTED_USER_STORAGE_KEY = "spend-analyzer:selected-user";
const ALL = "all";

function parseRaw(raw: string | null, users: readonly User[]): number | null | undefined {
  if (raw === null) return undefined;
  if (raw === ALL) return null;
  const id = Number(raw);
  if (!Number.isInteger(id)) return undefined;
  return users.some((u) => u.id === id) ? id : undefined;
}

/**
 * Pure resolution of the selected user. Precedence: explicit URL value, then the stored value,
 * then the default "All users". With exactly one user, that user always wins.
 * Returns a user id, or `null` for "All users". Ids not present in `users` (e.g. deleted) are
 * ignored and fall through to the next rule.
 */
export function resolveSelectedUser(
  urlParam: string | null,
  storedValue: string | null,
  users: readonly User[],
): number | null {
  // With exactly one user there is nothing to switch between (and the switcher is hidden), so
  // "All users" would be a dead end: always show that user.
  if (users.length === 1) return users[0]!.id;
  const fromUrl = parseRaw(urlParam, users);
  if (fromUrl !== undefined) return fromUrl;
  const fromStore = parseRaw(storedValue, users);
  if (fromStore !== undefined) return fromStore;
  return users.length === 1 ? users[0]!.id : null;
}

export function readStoredUser(): string | null {
  try {
    return window.localStorage.getItem(SELECTED_USER_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function writeStoredUser(value: string): void {
  try {
    window.localStorage.setItem(SELECTED_USER_STORAGE_KEY, value);
  } catch {
    // Ignore: a convenience, not a requirement.
  }
}

/** URL search param holding the compare-mode second pane's user: `?compare=<id>` or `?compare=all`. */
export const COMPARE_USER_PARAM = "compare";

/**
 * Pane override for compare mode. The shell renders a screen twice, each inside a provider that
 * pins the user for that pane; `useSelectedUser()` then reads this instead of the URL.
 */
export interface SelectedUserPane {
  /** The pane's fixed user id (`null` = All users). */
  userId: number | null;
  setUserId: (id: number | null) => void;
  /** Whether this pane owns global keyboard shortcuts (only one pane does). */
  active: boolean;
}

const SelectedUserPaneContext = React.createContext<SelectedUserPane | null>(null);
export const SelectedUserPaneProvider = SelectedUserPaneContext.Provider;

/** The enclosing compare pane, or `null` when the screen is rendered on its own. */
export function useSelectedUserPane(): SelectedUserPane | null {
  return React.useContext(SelectedUserPaneContext);
}

/**
 * Whether this screen instance should handle document-level keyboard shortcuts: always, except
 * in compare mode where only the active pane does.
 */
export function usePaneShortcutsEnabled(): boolean {
  const pane = useSelectedUserPane();
  return pane === null || pane.active;
}

export interface UseSelectedUser {
  users: User[];
  isLoading: boolean;
  error: unknown;
  /** `null` means "All users". */
  selectedUserId: number | null;
  selectedUser: User | null;
  /** `[id]` when one user is selected, else `undefined` — drops straight into `user_ids` queries. */
  userIds: number[] | undefined;
  setSelectedUserId: (id: number | null) => void;
}

const NO_USERS: User[] = [];

/** Shared per-user view selection for every per-user screen (URL + localStorage). */
export function useSelectedUser(): UseSelectedUser {
  const usersQuery = useUsers();
  const [searchParams, setSearchParams] = useSearchParams();
  const users = usersQuery.data ?? NO_USERS;
  const isLoading = usersQuery.isLoading;
  const urlParam = searchParams.get(SELECTED_USER_PARAM);
  const pane = useSelectedUserPane();

  const urlSelectedUserId = React.useMemo(
    () => (isLoading ? null : resolveSelectedUser(urlParam, readStoredUser(), users)),
    [isLoading, urlParam, users],
  );
  const selectedUserId = pane ? pane.userId : urlSelectedUserId;

  const setUrlSelectedUserId = React.useCallback(
    (id: number | null) => {
      const value = id === null ? ALL : String(id);
      writeStoredUser(value);
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set(SELECTED_USER_PARAM, value);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );
  const setSelectedUserId = pane ? pane.setUserId : setUrlSelectedUserId;

  const selectedUser = React.useMemo(
    () => users.find((u) => u.id === selectedUserId) ?? null,
    [users, selectedUserId],
  );
  const userIds = React.useMemo(
    () => (selectedUserId === null ? undefined : [selectedUserId]),
    [selectedUserId],
  );

  return {
    users,
    isLoading,
    error: usersQuery.error,
    selectedUserId,
    selectedUser,
    userIds,
    setSelectedUserId,
  };
}

export interface UseCompareUser {
  /** Whether a valid second pane is active (two or more users, `?compare=` differs from the primary). */
  active: boolean;
  /** The second pane's user (`null` = All users); only meaningful when `active`. */
  compareUserId: number | null;
  /** Turn compare on with a default second user, or off. */
  setEnabled: (enabled: boolean) => void;
  setCompareUserId: (id: number | null) => void;
  /** Set both panes in one URL update (used to swap them). */
  setPair: (primary: number | null, compare: number | null) => void;
}

/** URL-backed compare state for the shell. `primaryId` is the left pane's (URL-resolved) user. */
export function useCompareUser(primaryId: number | null, users: readonly User[], isLoading: boolean): UseCompareUser {
  const [searchParams, setSearchParams] = useSearchParams();
  const raw = searchParams.get(COMPARE_USER_PARAM);
  const parsed = isLoading ? undefined : parseRaw(raw, users);
  const active = users.length > 1 && parsed !== undefined && parsed !== primaryId;

  // A `?compare=` that cannot be shown (the primary pane's own user, or too few users) is dropped
  // from the URL so the address never claims a comparison that is not on screen.
  React.useEffect(() => {
    if (isLoading || raw === null || active) return;
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete(COMPARE_USER_PARAM);
        return next;
      },
      { replace: true },
    );
  }, [isLoading, raw, active, setSearchParams]);

  const write = React.useCallback(
    (value: string | null) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (value === null) next.delete(COMPARE_USER_PARAM);
          else next.set(COMPARE_USER_PARAM, value);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const setEnabled = React.useCallback(
    (enabled: boolean) => {
      if (!enabled) return write(null);
      const other = users.find((u) => u.id !== primaryId) ?? null;
      write(other ? String(other.id) : null);
    },
    [users, primaryId, write],
  );
  const setCompareUserId = React.useCallback((id: number | null) => write(id === null ? ALL : String(id)), [write]);

  const setPair = React.useCallback(
    (primary: number | null, compareId: number | null) => {
      const p = primary === null ? ALL : String(primary);
      writeStoredUser(p);
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set(SELECTED_USER_PARAM, p);
          next.set(COMPARE_USER_PARAM, compareId === null ? ALL : String(compareId));
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  return { active, compareUserId: active ? parsed : null, setEnabled, setCompareUserId, setPair };
}
