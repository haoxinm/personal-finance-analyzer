import * as React from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { LoadingState } from "@/components/primitives/states";
import { useToast } from "@/components/ui/use-toast";

import { hostnameOf, isLocalAddress, resolveUrl } from "../lib/llm-address";
import {
  type LlmSettings,
  type Settings,
  type TestLlmResponse,
  useClearApiKey,
  useSecretStorageQuery,
  useSetApiKey,
  useSetFileStore,
  useTestLlm,
} from "../lib/hooks";
import { cannotSaveKey, describeKeySource } from "../lib/secret-storage";

type ApiFormat = LlmSettings["api_format"];
type Extractor = Settings["ingest"]["default_extractor"];

const FORMATS: { value: ApiFormat; label: string; defaultPath: string }[] = [
  { value: "openai_chat", label: "OpenAI Chat Completions", defaultPath: "/v1/chat/completions" },
  { value: "openai_responses", label: "OpenAI Responses", defaultPath: "/v1/responses" },
  { value: "anthropic_messages", label: "Anthropic Messages", defaultPath: "/v1/messages" },
];

const PRESETS: { label: string; base_url: string; api_format: ApiFormat }[] = [
  { label: "Ollama", base_url: "http://localhost:11434", api_format: "openai_chat" },
  { label: "LM Studio", base_url: "http://localhost:1234", api_format: "openai_chat" },
  { label: "OpenAI", base_url: "https://api.openai.com", api_format: "openai_chat" },
  { label: "OpenAI Responses", base_url: "https://api.openai.com", api_format: "openai_responses" },
  { label: "Anthropic", base_url: "https://api.anthropic.com", api_format: "anthropic_messages" },
  { label: "OpenRouter", base_url: "https://openrouter.ai/api", api_format: "openai_chat" },
];

const REASONING_EFFORTS = ["", "minimal", "low", "medium", "high", "xhigh", "max"];

function defaultPathFor(format: ApiFormat): string {
  return FORMATS.find((f) => f.value === format)?.defaultPath ?? "";
}

function formatExtraBody(value: LlmSettings["extra_body"]): string {
  return value && Object.keys(value).length > 0 ? JSON.stringify(value, null, 2) : "";
}

function formatHeaders(value: LlmSettings["extra_headers"]): string {
  return Object.entries(value ?? {})
    .map(([k, v]) => `${k}: ${v}`)
    .join("\n");
}

function parseExtraBody(text: string): { value: Record<string, unknown>; error: string | null } {
  if (!text.trim()) return { value: {}, error: null };
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { value: {}, error: "Extra request body must be a JSON object, e.g. {\"top_p\": 0.9}." };
    }
    return { value: parsed as Record<string, unknown>, error: null };
  } catch (err) {
    return { value: {}, error: `Not valid JSON: ${err instanceof Error ? err.message : String(err)}` };
  }
}

function parseHeaders(text: string): { value: Record<string, string>; error: string | null } {
  const value: Record<string, string> = {};
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    const idx = line.indexOf(":");
    if (idx <= 0) return { value: {}, error: `Header line “${line.trim()}” must look like Key: Value.` };
    value[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
  }
  return { value, error: null };
}

function numberOrNull(raw: string): number | null {
  if (raw.trim() === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

export interface LlmSettingsSectionProps {
  settings: Settings;
  onSave: (llm: LlmSettings, defaultExtractor: Extractor, taxLlmFallback: boolean) => void;
  saving: boolean;
}

/** One unified LLM endpoint: the address decides local vs remote. The API key field is
 * write-only (I7): it is never fetched, never rendered, and the input is cleared on save. */
export function LlmSettingsSection({ settings, onSave, saving }: LlmSettingsSectionProps) {
  const { toast } = useToast();
  const [draft, setDraft] = React.useState<LlmSettings>(settings.llm);
  const [extractor, setExtractor] = React.useState<Extractor>(settings.ingest.default_extractor);
  const [taxFallback, setTaxFallback] = React.useState(settings.ingest.tax_llm_fallback ?? false);
  const [bodyText, setBodyText] = React.useState(formatExtraBody(settings.llm.extra_body));
  const [headersText, setHeadersText] = React.useState(formatHeaders(settings.llm.extra_headers));
  const [apiKeyInput, setApiKeyInput] = React.useState("");
  const [testResult, setTestResult] = React.useState<TestLlmResponse | null>(null);
  const setApiKey = useSetApiKey();
  const clearApiKey = useClearApiKey();
  const testLlm = useTestLlm();
  const setFileStore = useSetFileStore();

  React.useEffect(() => {
    setDraft(settings.llm);
    setBodyText(formatExtraBody(settings.llm.extra_body));
    setHeadersText(formatHeaders(settings.llm.extra_headers));
  }, [settings.llm]);

  React.useEffect(() => {
    setExtractor(settings.ingest.default_extractor);
  }, [settings.ingest.default_extractor]);

  React.useEffect(() => {
    setTaxFallback(settings.ingest.tax_llm_fallback ?? false);
  }, [settings.ingest.tax_llm_fallback]);

  const body = parseExtraBody(bodyText);
  const headers = parseHeaders(headersText);
  const effective: LlmSettings = { ...draft, extra_body: body.value, extra_headers: headers.value };
  const invalid = body.error !== null || headers.error !== null;

  const saved: LlmSettings = {
    ...settings.llm,
    extra_body: settings.llm.extra_body ?? {},
    extra_headers: settings.llm.extra_headers ?? {},
  };
  const dirty =
    JSON.stringify(effective) !== JSON.stringify(saved) ||
    extractor !== settings.ingest.default_extractor ||
    taxFallback !== (settings.ingest.tax_llm_fallback ?? false);

  const update = <K extends keyof LlmSettings>(key: K, value: LlmSettings[K]) => {
    setDraft((prev) => ({ ...prev, [key]: value }));
  };

  const local = isLocalAddress(draft.base_url);
  const host = hostnameOf(draft.base_url);
  const previewUrl = resolveUrl(draft.base_url, draft.api_format, draft.path);
  const hasAddress = draft.base_url.trim() !== "";
  const keyBase = draft.base_url.trim();
  const secretStorage = useSecretStorageQuery(settings.llm.base_url.trim());
  const secretStatus = secretStorage.data;
  const noSecureStorage = cannotSaveKey(secretStatus);

  const handleSaveKey = () => {
    if (!keyBase || !apiKeyInput) return;
    setApiKey.mutate(
      { baseUrl: keyBase, apiKey: apiKeyInput },
      {
        onSuccess: () => {
          setApiKeyInput("");
          toast({
            title:
              secretStatus?.storage === "file"
                ? "API key saved to the plaintext key file"
                : "API key saved to the credential store",
          });
        },
        onError: (err) => toast({ title: "Could not save API key", description: String(err), variant: "destructive" }),
      },
    );
  };

  const handleClearKey = () => {
    if (!keyBase) return;
    clearApiKey.mutate(
      { baseUrl: keyBase },
      {
        onSuccess: () => toast({ title: "API key removed" }),
        onError: (err) => toast({ title: "Could not remove API key", description: String(err), variant: "destructive" }),
      },
    );
  };

  const handleToggleFileStore = (allow: boolean) => {
    setFileStore.mutate(allow, {
      onError: (err) =>
        toast({ title: "Could not change the key file setting", description: String(err), variant: "destructive" }),
    });
  };

  const handleTest = () => {
    setTestResult(null);
    testLlm.mutate(effective, {
      onSuccess: (result) => {
        setTestResult(result ?? null);
        toast({
          title: result?.ok ? "LLM round trip succeeded" : "LLM round trip failed",
          description: result?.detail ?? undefined,
          variant: result?.ok ? "success" : "destructive",
        });
      },
      onError: (err) => toast({ title: "Could not test LLM", description: String(err), variant: "destructive" }),
    });
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>LLM provider</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <label className="flex items-center gap-2 text-sm font-medium">
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(e) => update("enabled", e.target.checked)}
          />
          Use an LLM
        </label>
        {!draft.enabled ? (
          <p className="text-xs text-muted-foreground">
            Off: nothing is ever sent anywhere. Unmatched merchants land in Needs Review for you to classify by hand.
            This is a fully supported mode, not a degraded one.
          </p>
        ) : (
          <>
            <p className="text-xs text-muted-foreground">
              Classification sends only cleaned merchant descriptions. Statement parsing (chosen per statement at
              import) sends the selected pages’ text, after masking account numbers and emails. Either way it goes only
              to the address below.
            </p>

            <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Presets">
              <span className="text-xs text-muted-foreground">Presets</span>
              {PRESETS.map((preset) => (
                <Button
                  key={preset.label}
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => setDraft((prev) => ({ ...prev, base_url: preset.base_url, api_format: preset.api_format }))}
                >
                  {preset.label}
                </Button>
              ))}
            </div>

            <div className="flex flex-wrap gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-xs text-muted-foreground">Address</span>
                <Input
                  value={draft.base_url}
                  onChange={(e) => update("base_url", e.target.value)}
                  placeholder="http://localhost:11434"
                  className="w-72"
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-muted-foreground">API format</span>
                <select
                  className="h-9 w-56 rounded-md border border-input bg-transparent px-2 text-sm"
                  value={draft.api_format}
                  onChange={(e) => update("api_format", e.target.value as ApiFormat)}
                >
                  {FORMATS.map((f) => (
                    <option key={f.value} value={f.value}>
                      {f.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-muted-foreground">Path</span>
                <Input
                  value={draft.path}
                  onChange={(e) => update("path", e.target.value)}
                  placeholder={defaultPathFor(draft.api_format)}
                  className="w-56"
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-muted-foreground">Model</span>
                <Input
                  value={draft.model}
                  onChange={(e) => update("model", e.target.value)}
                  placeholder="e.g. llama3.1 — optional for single-model local servers"
                  className="w-48"
                />
                <span className="max-w-48 text-xs text-muted-foreground" data-testid="model-hint">
                  Leave blank if your local server has one model loaded. OpenAI and Anthropic require one.
                </span>
              </label>
            </div>

            {hasAddress ? (
              <div className="flex flex-col gap-1">
                <div>
                  <Badge variant={local ? "default" : "secondary"} data-testid="locality-badge">
                    {local ? "Local — nothing leaves this computer" : `Remote — data is sent to ${host || draft.base_url}`}
                  </Badge>
                </div>
                <p className="text-xs text-muted-foreground">
                  Requests go to: <code>{previewUrl}</code>
                </p>
              </div>
            ) : null}

            <details className="rounded-md border border-border p-3">
              <summary className="cursor-pointer text-sm font-medium">Advanced</summary>
              <div className="mt-3 flex flex-col gap-3">
                <div className="flex flex-wrap gap-3">
                  <label className="flex flex-col gap-1">
                    <span className="text-xs text-muted-foreground">Temperature (empty = not sent)</span>
                    <Input
                      type="number"
                      min={0}
                      step={0.1}
                      value={draft.temperature ?? ""}
                      onChange={(e) => update("temperature", numberOrNull(e.target.value))}
                      className="w-32"
                      aria-label="Temperature"
                    />
                  </label>
                  <label className="flex flex-col gap-1">
                    <span className="text-xs text-muted-foreground">Max tokens (empty = default)</span>
                    <Input
                      type="number"
                      min={1}
                      value={draft.max_tokens ?? ""}
                      onChange={(e) => update("max_tokens", numberOrNull(e.target.value))}
                      className="w-32"
                      aria-label="Max tokens"
                    />
                  </label>
                  <label className="flex flex-col gap-1">
                    <span className="text-xs text-muted-foreground">Reasoning effort</span>
                    <select
                      className="h-9 w-36 rounded-md border border-input bg-transparent px-2 text-sm"
                      value={draft.reasoning_effort}
                      onChange={(e) => update("reasoning_effort", e.target.value)}
                      aria-label="Reasoning effort"
                    >
                      {REASONING_EFFORTS.map((r) => (
                        <option key={r} value={r}>
                          {r === "" ? "Default" : r}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="flex flex-col gap-1">
                    <span className="text-xs text-muted-foreground">Batch size</span>
                    <Input
                      type="number"
                      min={1}
                      value={draft.batch_size}
                      onChange={(e) => update("batch_size", Number(e.target.value))}
                      className="w-24"
                      aria-label="Batch size"
                    />
                  </label>
                  <label className="flex flex-col gap-1">
                    <span className="text-xs text-muted-foreground">Timeout (s)</span>
                    <Input
                      type="number"
                      min={1}
                      value={draft.timeout_s}
                      onChange={(e) => update("timeout_s", Number(e.target.value))}
                      className="w-24"
                      aria-label="Timeout (s)"
                    />
                  </label>
                  <label className="flex flex-col gap-1">
                    <span className="text-xs text-muted-foreground">Confidence threshold</span>
                    <Input
                      type="number"
                      min={0}
                      max={1}
                      step={0.05}
                      value={draft.confidence_threshold}
                      onChange={(e) => update("confidence_threshold", Number(e.target.value))}
                      className="w-24"
                      aria-label="Confidence threshold"
                    />
                  </label>
                  <label className="flex flex-col gap-1">
                    <span className="text-xs text-muted-foreground">Pages per request (0 = all)</span>
                    <Input
                      type="number"
                      min={0}
                      value={draft.parse_pages_per_request}
                      onChange={(e) => update("parse_pages_per_request", Number(e.target.value))}
                      className="w-32"
                      aria-label="Pages per request"
                    />
                  </label>
                </div>

                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={draft.structured_output}
                    onChange={(e) => update("structured_output", e.target.checked)}
                  />
                  Structured output (ask the model for schema-constrained JSON)
                </label>

                <label className="flex flex-col gap-1">
                  <span className="text-xs text-muted-foreground">Extra request body (JSON object)</span>
                  <textarea
                    className="min-h-20 w-full rounded-md border border-input bg-transparent p-2 font-mono text-xs"
                    value={bodyText}
                    onChange={(e) => setBodyText(e.target.value)}
                    placeholder='{"top_p": 0.9}'
                    aria-label="Extra request body"
                    aria-invalid={body.error !== null}
                  />
                </label>
                {body.error ? (
                  <p role="alert" className="text-xs text-destructive">
                    {body.error}
                  </p>
                ) : null}

                <label className="flex flex-col gap-1">
                  <span className="text-xs text-muted-foreground">Extra headers (one “Key: Value” per line)</span>
                  <textarea
                    className="min-h-16 w-full rounded-md border border-input bg-transparent p-2 font-mono text-xs"
                    value={headersText}
                    onChange={(e) => setHeadersText(e.target.value)}
                    placeholder="X-Title: Personal Finance Analyzer"
                    aria-label="Extra headers"
                    aria-invalid={headers.error !== null}
                  />
                </label>
                {headers.error ? (
                  <p role="alert" className="text-xs text-destructive">
                    {headers.error}
                  </p>
                ) : null}
              </div>
            </details>

            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={extractor === "llm"}
                onChange={(e) => setExtractor(e.target.checked ? "llm" : "auto")}
              />
              Parse statements with the LLM by default
            </label>

            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                className="mt-1"
                checked={taxFallback}
                onChange={(e) => setTaxFallback(e.target.checked)}
              />
              <span>
                Use the LLM for tax forms the built-in parsers can&apos;t read
                <span className="block text-xs text-muted-foreground">
                  Send unreadable sections of tax forms to your LLM. SSNs, EINs and account numbers are masked to
                  the last 4 digits; names/addresses are removed best-effort. Values it reads are marked for
                  review. Off by default: tax forms otherwise never leave this computer.
                </span>
              </span>
            </label>

            <div className="flex flex-col gap-2 rounded-md border border-border p-3">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">API key {local ? "(optional for local)" : ""}</span>
                <Badge variant={draft.has_key ? "default" : "secondary"}>
                  {draft.has_key ? "Key on file" : "No key on file"}
                </Badge>
              </div>
              <p className="text-xs text-muted-foreground">
                Stored in your operating system’s credential store (macOS Keychain, Windows Credential Manager, or
                Linux Secret Service) and only ever sent to {host || "this address"}. Write-only: it is never fetched
                back or shown here.
              </p>
              {secretStatus ? (
                <div className="flex flex-col gap-2" data-testid="secret-storage">
                  <p className="text-xs" data-testid="secret-storage-source">
                    <span className="font-medium">Secret storage:</span>{" "}
                    {secretStatus.keychain_usable ? secretStatus.keychain_name : "no system keychain available"}
                    {" — "}
                    {describeKeySource(secretStatus)}.
                  </p>
                  {!secretStatus.keychain_usable ? (
                    <div
                      role="alert"
                      data-testid="no-keychain-guidance"
                      className="rounded-md border border-border bg-muted/40 p-3 text-xs"
                    >
                      <p className="font-medium">No usable system keychain on this machine</p>
                      <p className="text-muted-foreground">{secretStatus.keychain_detail}.</p>
                      <p className="mt-1">{secretStatus.guidance}</p>
                      <label className="mt-2 flex items-start gap-2">
                        <input
                          type="checkbox"
                          className="mt-0.5"
                          checked={secretStatus.file_store_enabled}
                          disabled={setFileStore.isPending || Boolean(secretStatus.file_store_env_var)}
                          onChange={(e) => handleToggleFileStore(e.target.checked)}
                          aria-label="Keep API keys in a plaintext file"
                        />
                        <span>
                          Keep API keys in a plaintext file when there is no keychain
                          <span className="block text-muted-foreground">
                            Plaintext at rest, protected only by file permissions (0600) — not encrypted and not
                            included in backups. Off by default; used only while no keychain is available.
                          </span>
                        </span>
                      </label>
                      {secretStatus.file_store_env_var ? (
                        <p data-testid="file-store-env-note" className="mt-1 text-muted-foreground">
                          This is set by the {secretStatus.file_store_env_var} environment variable, which takes
                          precedence over this setting. Unset it and restart to change it here.
                        </p>
                      ) : null}
                      {secretStatus.file_store_enabled ? (
                        <p data-testid="file-store-warning" className="mt-1 text-destructive">
                          {secretStatus.warning}
                        </p>
                      ) : null}
                    </div>
                  ) : secretStatus.file_store_has_keys ? (
                    <p className="text-xs text-muted-foreground">
                      A key is also saved in the plaintext key file ({secretStatus.file_store_path}); saving the key
                      again moves it into {secretStatus.keychain_name}.
                    </p>
                  ) : null}
                </div>
              ) : null}
              <div className="flex items-end gap-2">
                <label className="flex flex-col gap-1">
                  <span className="text-xs text-muted-foreground">New key for {host || "this address"}</span>
                  <Input
                    type="password"
                    autoComplete="off"
                    value={apiKeyInput}
                    onChange={(e) => setApiKeyInput(e.target.value)}
                    className="w-64"
                    aria-label="New API key"
                  />
                </label>
                <Button
                  size="sm"
                  onClick={handleSaveKey}
                  disabled={!keyBase || !apiKeyInput || setApiKey.isPending || noSecureStorage}
                >
                  Save key
                </Button>
                {draft.has_key ? (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={handleClearKey}
                    disabled={!keyBase || clearApiKey.isPending}
                  >
                    Remove key
                  </Button>
                ) : null}
              </div>
            </div>
          </>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={() => onSave(effective, extractor, taxFallback)} disabled={!dirty || saving || invalid}>
            Save LLM settings
          </Button>
          {draft.enabled ? (
            <Button variant="outline" onClick={handleTest} disabled={testLlm.isPending || invalid}>
              {testLlm.isPending ? "Testing…" : "Test connection"}
            </Button>
          ) : null}
          <span className="text-xs text-muted-foreground">
            Settings take effect immediately after Save — no restart needed.
          </span>
        </div>
        {testLlm.isPending ? <LoadingState rows={1} /> : null}
        {testResult ? (
          <div
            role="status"
            data-testid="test-result"
            className={`rounded-md border p-3 text-sm ${testResult.ok ? "border-border" : "border-destructive text-destructive"}`}
          >
            <p className="font-medium">{testResult.ok ? "Connection succeeded" : "Connection failed"}</p>
            {testResult.detail ? <p>{testResult.detail}</p> : null}
            {testResult.resolved_url ? (
              <p className="text-xs text-muted-foreground">
                Tried: <code>{testResult.resolved_url}</code> ({testResult.mode})
              </p>
            ) : null}
            {testResult.latency_ms != null ? (
              <p className="text-xs text-muted-foreground">{Math.round(testResult.latency_ms)} ms</p>
            ) : null}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
