import * as React from "react";
import { useLocation } from "react-router-dom";

import { Page, PageHeader } from "@/components/primitives/page";
import { ErrorState, LoadingState } from "@/components/primitives/states";
import { useToast } from "@/components/ui/use-toast";

import { BackupSection } from "./components/backup-section";
import { EgressPreviewSection } from "./components/egress-preview-section";
import { LlmSettingsSection } from "./components/llm-settings-section";
import { PrivacySection } from "./components/privacy-section";
import { TaxParametersSection } from "./components/tax-parameters-section";
import { UsersAccountsSection } from "./components/users-accounts-section";
import {
  type LlmSettings,
  type PrivacySettings,
  type Settings,
  type TaxSettings,
  useSettingsQuery,
  useUpdateSettings,
} from "./lib/hooks";

/** The sections in page order: `id` is the anchor (`/settings#users`), `label` the in-page nav text. */
const SECTION_LINKS = [
  { id: "users", label: "Users and accounts" },
  { id: "llm", label: "LLM provider" },
  { id: "privacy", label: "Privacy" },
  { id: "tax-parameters", label: "Tax parameters" },
  { id: "egress-preview", label: "Preview what will be sent" },
  { id: "backup", label: "Backup & restore" },
] as const;

/** A section's anchor target. `scroll-mt` clears the sticky header on wide screens. */
function Anchor({ id, children }: { id: (typeof SECTION_LINKS)[number]["id"]; children: React.ReactNode }) {
  return (
    <div id={id} className="scroll-mt-4 md:scroll-mt-40">
      {children}
    </div>
  );
}

/**
 * Settings screen (P3-E): users and accounts CRUD, the unified LLM endpoint with
 * its key/model/threshold/batch-size fields, privacy toggles, and the egress preview — "the
 * feature that earns user trust", per the plan — then backup & restore for moving to another machine.
 */
function SettingsScreen() {
  const { toast } = useToast();
  const settingsQuery = useSettingsQuery();
  const updateSettings = useUpdateSettings();

  // Arriving from another page with `#section` (the user bar's "Add one in Settings"): the router
  // does not scroll to a hash, and the sections only exist once the settings have loaded.
  const { hash } = useLocation();
  const loaded = settingsQuery.isSuccess;
  React.useEffect(() => {
    if (loaded && hash) document.getElementById(hash.slice(1))?.scrollIntoView();
  }, [loaded, hash]);

  const header = (
    <PageHeader
      title="Settings"
      description="Users, accounts, the LLM provider, privacy, tax parameters, exactly what would be sent to the LLM, and backup & restore."
    />
  );

  if (settingsQuery.isPending) {
    return (
      <Page>
        {header}
        <LoadingState rows={6} />
      </Page>
    );
  }
  if (settingsQuery.isError || !settingsQuery.data) {
    return (
      <Page>
        {header}
        <ErrorState
          title="Could not load settings"
          description={settingsQuery.isError ? String(settingsQuery.error) : undefined}
          onRetry={() => void settingsQuery.refetch()}
        />
      </Page>
    );
  }

  const settings = settingsQuery.data;

  const saveLlm = (
    llm: LlmSettings,
    defaultExtractor: Settings["ingest"]["default_extractor"],
    taxLlmFallback: boolean,
  ) => {
    updateSettings.mutate(
      { ...settings, llm, ingest: { ...settings.ingest, default_extractor: defaultExtractor, tax_llm_fallback: taxLlmFallback } },
      {
        onSuccess: () => toast({ title: "LLM settings saved" }),
        onError: (err) => toast({ title: "Could not save settings", description: String(err), variant: "destructive" }),
      },
    );
  };

  const savePrivacy = (privacy: PrivacySettings) => {
    updateSettings.mutate(
      { ...settings, privacy },
      {
        onSuccess: () => toast({ title: "Privacy settings saved" }),
        onError: (err) => toast({ title: "Could not save settings", description: String(err), variant: "destructive" }),
      },
    );
  };

  const saveTax = (tax: TaxSettings) => {
    updateSettings.mutate(
      { ...settings, tax },
      {
        onSuccess: () => toast({ title: "Tax settings saved" }),
        onError: (err) => toast({ title: "Could not save settings", description: String(err), variant: "destructive" }),
      },
    );
  };

  return (
    <Page>
      {header}
      <nav aria-label="On this page" className="-mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm">
        {SECTION_LINKS.map((link) => (
          <a
            key={link.id}
            href={`#${link.id}`}
            className="rounded-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
          >
            {link.label}
          </a>
        ))}
      </nav>
      <Anchor id="users">
        <UsersAccountsSection />
      </Anchor>
      <Anchor id="llm">
        <LlmSettingsSection settings={settings} onSave={saveLlm} saving={updateSettings.isPending} />
      </Anchor>
      <Anchor id="privacy">
        <PrivacySection settings={settings} onSave={savePrivacy} saving={updateSettings.isPending} />
      </Anchor>
      <Anchor id="tax-parameters">
        <TaxParametersSection settings={settings} onSave={saveTax} saving={updateSettings.isPending} />
      </Anchor>
      <Anchor id="egress-preview">
        <EgressPreviewSection settings={settings} />
      </Anchor>
      <Anchor id="backup">
        <BackupSection />
      </Anchor>
    </Page>
  );
}

export default SettingsScreen;

export const screenMeta = { path: "/settings", title: "Settings" };
