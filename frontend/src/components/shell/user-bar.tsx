import * as React from "react";

import type { User } from "@/hooks/use-selected-user";
import { Button } from "@/components/ui/button";
import { UserChip } from "@/components/primitives/user-chip";
import { UserSwitcher } from "@/components/primitives/user-switcher";

export interface UserBarProps {
  users: readonly User[];
  isLoading: boolean;
  /** Left / only pane's user (`null` = All users). */
  primaryId: number | null;
  onPrimaryChange: (id: number | null) => void;
  /** Whether the current route can render two panes. */
  compareSupported: boolean;
  compareActive: boolean;
  compareUserId: number | null;
  onCompareToggle: (enabled: boolean) => void;
  onCompareUserChange: (id: number | null) => void;
  /** Optional clarifying note, e.g. "Rules apply to all users". */
  note?: string;
}

function nameOf(users: readonly User[], id: number | null): string {
  return users.find((u) => u.id === id)?.name ?? "";
}

/**
 * The app-wide "who am I looking at" bar. Always visible (outside Settings): a large coloured
 * "Viewing: <name>" chip, the switcher when there is more than one user, and the compare toggle.
 */
export function UserBar({
  users,
  isLoading,
  primaryId,
  onPrimaryChange,
  compareSupported,
  compareActive,
  compareUserId,
  onCompareToggle,
  onCompareUserChange,
  note,
}: UserBarProps) {
  const multiple = users.length > 1;
  const compareTitle = compareSupported
    ? undefined
    : "Compare is available on Spending (Overview, Transactions, Review), Income & Tax (Overview, Documents) and Net worth.";

  let body: React.ReactNode;
  if (isLoading) {
    body = <span className="text-sm text-muted-foreground">Loading users…</span>;
  } else if (users.length === 0) {
    body = <span className="text-sm text-muted-foreground">No users yet. Add one in Settings.</span>;
  } else if (compareActive) {
    body = (
      <>
        <PaneSelector
          label="Left"
          users={users}
          value={primaryId}
          onChange={onPrimaryChange}
          switcherLabel="Left user"
        />
        <PaneSelector
          label="Right"
          users={users}
          value={compareUserId}
          onChange={onCompareUserChange}
          switcherLabel="Right user"
        />
        <Button type="button" variant="outline" size="sm" onClick={() => onCompareToggle(false)}>
          Stop comparing
        </Button>
        <span className="text-xs text-muted-foreground">Filters apply to both.</span>
      </>
    );
  } else {
    body = (
      <>
        <div className="flex items-center gap-2" aria-live="polite">
          <span className="text-sm font-medium text-muted-foreground">Viewing:</span>
          <UserChip userId={primaryId} name={nameOf(users, primaryId)} size="lg" />
        </div>
        {multiple ? (
          <UserSwitcher
            users={users}
            value={primaryId}
            onChange={onPrimaryChange}
            avatars
            aria-label="Switch user"
          />
        ) : null}
        {multiple ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={!compareSupported}
            title={compareTitle}
            aria-pressed={false}
            onClick={() => onCompareToggle(true)}
          >
            Compare
          </Button>
        ) : null}
      </>
    );
  }

  return (
    <div role="region" aria-label="Selected user" data-testid="user-bar" className="border-b border-border bg-muted/40">
      <div className="mx-auto flex max-w-screen-2xl flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2">
        {body}
        {note ? <span className="text-xs text-muted-foreground">{note}</span> : null}
      </div>
    </div>
  );
}

function PaneSelector({
  label,
  users,
  value,
  onChange,
  switcherLabel,
}: {
  label: string;
  users: readonly User[];
  value: number | null;
  onChange: (id: number | null) => void;
  switcherLabel: string;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-sm font-medium text-muted-foreground">{label}:</span>
      <UserChip userId={value} name={nameOf(users, value)} size="md" />
      <UserSwitcher users={users} value={value} onChange={onChange} avatars aria-label={switcherLabel} />
    </div>
  );
}
