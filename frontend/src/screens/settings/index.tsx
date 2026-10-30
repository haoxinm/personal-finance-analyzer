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

/**
 * Settings screen (P3-E): users and accounts CRUD, the unified LLM endpoint with
 * its key/model/threshold/batch-size fields, privacy toggles, and the egress preview — "the
 * feature that earns user trust", per the plan — then backup & restore for moving to another machine.
 */
function SettingsScreen() {
  const { toast } = useToast();
  const settingsQuery = useSettingsQuery();
  const updateSettings = useUpdateSettings();

  if (settingsQuery.isPending) {
    return <LoadingState rows={6} />;
  }
  if (settingsQuery.isError || !settingsQuery.data) {
    return (
      <ErrorState
        title="Could not load settings"
        description={settingsQuery.isError ? String(settingsQuery.error) : undefined}
        onRetry={() => void settingsQuery.refetch()}
      />
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
    <div className="flex flex-col gap-6 py-2">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Users, accounts, the LLM provider, privacy, tax parameters, exactly what would be sent to the LLM, and
          backup & restore.
        </p>
      </div>
      <UsersAccountsSection />
      <LlmSettingsSection settings={settings} onSave={saveLlm} saving={updateSettings.isPending} />
      <PrivacySection settings={settings} onSave={savePrivacy} saving={updateSettings.isPending} />
      <TaxParametersSection settings={settings} onSave={saveTax} saving={updateSettings.isPending} />
      <EgressPreviewSection settings={settings} />
      <BackupSection />
    </div>
  );
}

export default SettingsScreen;

export const screenMeta = { path: "/settings", title: "Settings" };
