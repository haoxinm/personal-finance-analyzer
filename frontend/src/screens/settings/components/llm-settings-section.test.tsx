import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ToastContextProvider } from "@/components/ui/toast-provider";

import { isLocalAddress, resolveUrl } from "../lib/llm-address";
import { LlmSettingsSection } from "./llm-settings-section";
import type { SecretStorageStatus, Settings } from "../lib/hooks";

const setApiKeyMutate = vi.fn();
const clearApiKeyMutate = vi.fn();
const testLlmMutate = vi.fn();
const setFileStoreMutate = vi.fn();
const checkLegacyArgs: boolean[] = [];

const keychainStatus: SecretStorageStatus = {
  key_source: "keychain",
  storage: "keychain",
  keychain_name: "macOS Keychain",
  keychain_usable: true,
  keychain_detail: "",
  env_var: null,
  key_legacy: false,
  file_store_enabled: false,
  file_store_path: "/data/secrets/api_keys.toml",
  file_store_has_keys: false,
  legacy_checked: true,
  file_key_outcome: "none",
  moved_keys: 0,
  kept_keys: 0,
  guidance: "",
  warning: "",
};
let secretStatus: SecretStorageStatus | undefined = keychainStatus;

vi.mock("../lib/hooks", () => ({
  useSetApiKey: () => ({ mutate: setApiKeyMutate, isPending: false }),
  useClearApiKey: () => ({ mutate: clearApiKeyMutate, isPending: false }),
  useSecretStorageQuery: (_baseUrl: string, checkLegacy = false) => {
    checkLegacyArgs.push(checkLegacy);
    return { data: secretStatus };
  },
  useSetFileStore: () => ({ mutate: setFileStoreMutate, isPending: false }),
  useTestLlm: () => ({ mutate: testLlmMutate, isPending: false, isError: false, error: null }),
}));

const offSettings: Settings = {
  llm: {
    enabled: false,
    base_url: "",
    api_format: "openai_chat",
    path: "",
    model: "",
    temperature: null,
    max_tokens: null,
    reasoning_effort: "",
    extra_body: {},
    extra_headers: {},
    structured_output: true,
    batch_size: 50,
    timeout_s: 120,
    confidence_threshold: 0.7,
    parse_pages_per_request: 0,
    mode: "none",
    resolved_url: "",
    has_key: false,
  },
  privacy: { store_pdf_copies: true, store_extract_cache: false, pii_terms: [] },
  ingest: { always_confirm_extractor: false, default_extractor: "auto", tax_llm_fallback: false, date_format_hints: [], default_currency: "USD" },
  server: { port: 8756 },
};

const remoteSettings: Settings = {
  ...offSettings,
  llm: {
    ...offSettings.llm,
    enabled: true,
    base_url: "https://api.anthropic.com",
    api_format: "anthropic_messages",
    model: "claude",
    mode: "remote",
    has_key: true,
  },
};

function renderSection(settings: Settings, onSave = vi.fn()) {
  return render(
    <ToastContextProvider>
      <LlmSettingsSection settings={settings} onSave={onSave} saving={false} />
    </ToastContextProvider>,
  );
}

describe("address helpers", () => {
  it("classifies local vs remote like the backend", () => {
    expect(isLocalAddress("http://localhost:11434")).toBe(true);
    expect(isLocalAddress("http://127.0.0.1:1234/v1")).toBe(true);
    expect(isLocalAddress("http://[::1]:8000")).toBe(true);
    expect(isLocalAddress("http://foo.localhost")).toBe(true);
    expect(isLocalAddress("https://api.openai.com")).toBe(false);
    expect(isLocalAddress("")).toBe(false);
  });

  it("resolves the URL without doubling /v1", () => {
    expect(resolveUrl("https://api.openai.com/", "openai_chat", "")).toBe("https://api.openai.com/v1/chat/completions");
    expect(resolveUrl("http://x/v1", "openai_chat", "")).toBe("http://x/v1/chat/completions");
    expect(resolveUrl("http://x", "anthropic_messages", "/custom")).toBe("http://x/custom");
  });
});

describe("LlmSettingsSection", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("hides fields when off and shows them when the toggle is on", async () => {
    const user = userEvent.setup();
    renderSection(offSettings);
    expect(screen.queryByLabelText("Address")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /test connection/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /use an llm/i }));
    expect(screen.getByLabelText("Address")).toBeInTheDocument();
    expect(screen.getByLabelText("API format")).toBeInTheDocument();
    expect(screen.getByLabelText("Path")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /test connection/i })).toBeInTheDocument();
    expect(screen.getByText(/no restart needed/i)).toBeInTheDocument();
  });

  it("a preset fills address and API format and updates the badge", async () => {
    const user = userEvent.setup();
    renderSection({ ...offSettings, llm: { ...offSettings.llm, enabled: true } });
    await user.click(screen.getByRole("button", { name: "Ollama" }));
    expect(screen.getByLabelText("Address")).toHaveValue("http://localhost:11434");
    expect(screen.getByLabelText("API format")).toHaveValue("openai_chat");
    expect(screen.getByTestId("locality-badge")).toHaveTextContent("Local — nothing leaves this computer");
    expect(screen.getByText("http://localhost:11434/v1/chat/completions")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Anthropic" }));
    expect(screen.getByLabelText("API format")).toHaveValue("anthropic_messages");
    expect(screen.getByTestId("locality-badge")).toHaveTextContent("Remote — data is sent to api.anthropic.com");
    expect(screen.getByLabelText("Path")).toHaveAttribute("placeholder", "/v1/messages");
  });

  it("Test connection posts the unsaved draft", async () => {
    const user = userEvent.setup();
    testLlmMutate.mockImplementation(
      (_draft, opts: { onSuccess?: (r: unknown) => void }) =>
        opts.onSuccess?.({ ok: false, detail: "boom", mode: "local", resolved_url: "http://localhost:1234/v1/chat/completions", latency_ms: 12 }),
    );
    renderSection(remoteSettings);
    await user.clear(screen.getByLabelText("Address"));
    await user.type(screen.getByLabelText("Address"), "http://localhost:1234");
    await user.click(screen.getByRole("button", { name: /test connection/i }));

    expect(testLlmMutate).toHaveBeenCalledTimes(1);
    const draft = testLlmMutate.mock.calls[0]?.[0] as Settings["llm"];
    expect(draft).toMatchObject({ enabled: true, base_url: "http://localhost:1234", api_format: "anthropic_messages" });
    expect(await screen.findByTestId("test-result")).toHaveTextContent("boom");
    expect(screen.getByTestId("test-result")).toHaveTextContent("12 ms");
  });

  it("invalid extra body JSON shows an error and blocks save", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    renderSection(remoteSettings, onSave);
    const textarea = screen.getByLabelText("Extra request body");
    await user.click(textarea);
    await user.paste("[1, 2]");
    expect(screen.getByRole("alert")).toHaveTextContent(/must be a json object/i);
    expect(screen.getByRole("button", { name: /save llm settings/i })).toBeDisabled();

    await user.clear(textarea);
    await user.paste('{"top_p": 0.5}');
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /save llm settings/i }));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ extra_body: { top_p: 0.5 } }), "auto", false);
  });

  it("saves the default-extractor checkbox together with the LLM settings", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    renderSection(remoteSettings, onSave);
    expect(screen.getByRole("button", { name: /save llm settings/i })).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: /parse statements with the llm by default/i }));
    await user.click(screen.getByRole("button", { name: /save llm settings/i }));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ model: "claude" }), "llm", false);
  });

  it("saves the tax LLM fallback opt-in (off by default) with privacy copy", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    renderSection(remoteSettings, onSave);
    const box = screen.getByRole("checkbox", { name: /use the llm for tax forms/i });
    expect(box).not.toBeChecked();
    expect(screen.getByText(/masked to the last 4 digits/i)).toBeInTheDocument();
    await user.click(box);
    await user.click(screen.getByRole("button", { name: /save llm settings/i }));
    expect(onSave).toHaveBeenCalledWith(expect.anything(), "auto", true);
  });

  it("never displays the existing API key, only whether one is on file", () => {
    renderSection(remoteSettings);
    expect(screen.getByText("Key on file")).toBeInTheDocument();
    expect(screen.getByLabelText("New API key")).toHaveValue("");
    expect(screen.getByLabelText("New API key")).toHaveAttribute("type", "password");
  });

  it("saves a new API key for the draft address and clears the input", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    setApiKeyMutate.mockImplementation((_vars, opts: { onSuccess?: () => void }) => opts.onSuccess?.());
    renderSection(remoteSettings, onSave);

    await user.type(screen.getByLabelText("New API key"), "sk-new-secret");
    await user.click(screen.getByRole("button", { name: /save key/i }));

    expect(setApiKeyMutate).toHaveBeenCalledWith(
      { baseUrl: "https://api.anthropic.com", apiKey: "sk-new-secret" },
      expect.anything(),
    );
    expect(onSave).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByLabelText("New API key")).toHaveValue(""));
  });

  it("disables key save without an address", async () => {
    const user = userEvent.setup();
    renderSection({ ...offSettings, llm: { ...offSettings.llm, enabled: true } });
    await user.type(screen.getByLabelText("New API key"), "k");
    expect(screen.getByRole("button", { name: /save key/i })).toBeDisabled();
  });
});

describe("LlmSettingsSection secret storage", () => {
  afterEach(() => {
    secretStatus = keychainStatus;
    setFileStoreMutate.mockReset();
    setApiKeyMutate.mockReset();
  });

  it("names the keychain in use and where the key came from", () => {
    renderSection(remoteSettings);
    expect(screen.getByTestId("secret-storage-source")).toHaveTextContent("macOS Keychain");
    expect(screen.getByTestId("secret-storage-source")).toHaveTextContent("Key stored in macOS Keychain");
    expect(screen.queryByTestId("no-keychain-guidance")).not.toBeInTheDocument();
  });

  it("says when an environment variable supplies the key", () => {
    secretStatus = { ...keychainStatus, key_source: "env", env_var: "PFA_LLM_API_KEY" };
    renderSection(remoteSettings);
    expect(screen.getByTestId("secret-storage-source")).toHaveTextContent("PFA_LLM_API_KEY");
  });

  it("with no keychain: shows setup guidance, the file-store toggle off, and blocks saving", async () => {
    const user = userEvent.setup();
    secretStatus = {
      ...keychainStatus,
      key_source: "none",
      storage: "none",
      keychain_name: "none",
      keychain_usable: false,
      keychain_detail: "no usable keyring backend is installed or running",
      guidance: "Linux: install and start a Secret Service provider (gnome-keyring).",
    };
    renderSection(remoteSettings);

    const guidance = screen.getByTestId("no-keychain-guidance");
    expect(guidance).toHaveTextContent("No usable system keychain");
    expect(guidance).toHaveTextContent("gnome-keyring");
    expect(guidance).toHaveTextContent("plaintext");
    const toggle = screen.getByRole("checkbox", { name: "Keep API keys in a plaintext file" });
    expect(toggle).not.toBeChecked();
    expect(screen.queryByTestId("file-store-warning")).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("New API key"), "sk-secret");
    expect(screen.getByRole("button", { name: "Save key" })).toBeDisabled();

    await user.click(toggle);
    expect(setFileStoreMutate).toHaveBeenCalledWith({ allow: true }, expect.anything());
  });

  const noKeychainWithKey: SecretStorageStatus = {
    ...keychainStatus,
    key_source: "none",
    storage: "file",
    keychain_name: "none",
    keychain_usable: false,
    keychain_detail: "no usable keyring backend is installed or running",
    file_store_enabled: true,
    file_store_has_keys: true,
    guidance: "Linux: install gnome-keyring",
    warning: "File store: API keys are saved as plaintext in /data/secrets/api_keys.toml.",
  };

  it("turning the plaintext file off with a key in it asks what to do, defaulting to delete", async () => {
    const user = userEvent.setup();
    secretStatus = noKeychainWithKey;
    renderSection(remoteSettings);

    await user.click(screen.getByRole("checkbox", { name: "Keep API keys in a plaintext file" }));
    expect(setFileStoreMutate).not.toHaveBeenCalled(); // nothing happens until the user chooses

    const choice = screen.getByTestId("plaintext-key-choice");
    expect(choice).toHaveTextContent("/data/secrets/api_keys.toml");
    expect(screen.getByRole("radio", { name: /Delete the key file/ })).toBeChecked();
    expect(screen.queryByRole("radio", { name: /Move it into/ })).not.toBeInTheDocument(); // no keychain
    expect(screen.getByRole("radio", { name: /Keep the file on disk/ })).not.toBeChecked();

    await user.click(screen.getByRole("button", { name: "Delete the key file" }));
    expect(setFileStoreMutate).toHaveBeenCalledWith({ allow: false, action: "delete" }, expect.anything());
  });

  it("lets the user keep the file instead, explicitly", async () => {
    const user = userEvent.setup();
    secretStatus = noKeychainWithKey;
    renderSection(remoteSettings);
    await user.click(screen.getByRole("checkbox", { name: "Keep API keys in a plaintext file" }));
    await user.click(screen.getByRole("radio", { name: /Keep the file on disk/ }));
    await user.click(screen.getByRole("button", { name: "Keep the file" }));
    expect(setFileStoreMutate).toHaveBeenCalledWith({ allow: false, action: "keep" }, expect.anything());
  });

  it("cancelling the choice changes nothing", async () => {
    const user = userEvent.setup();
    secretStatus = noKeychainWithKey;
    renderSection(remoteSettings);
    await user.click(screen.getByRole("checkbox", { name: "Keep API keys in a plaintext file" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByTestId("plaintext-key-choice")).not.toBeInTheDocument();
    expect(setFileStoreMutate).not.toHaveBeenCalled();
  });

  it("turning the file off with no key in it needs no question", async () => {
    const user = userEvent.setup();
    secretStatus = { ...noKeychainWithKey, file_store_has_keys: false };
    renderSection(remoteSettings);
    await user.click(screen.getByRole("checkbox", { name: "Keep API keys in a plaintext file" }));
    expect(setFileStoreMutate).toHaveBeenCalledWith({ allow: false }, expect.anything());
  });

  it("says a key is still on disk after the file was turned off, and offers to delete it", async () => {
    const user = userEvent.setup();
    secretStatus = { ...noKeychainWithKey, file_store_enabled: false, storage: "none", warning: "" };
    renderSection(remoteSettings);

    const leftover = screen.getByTestId("file-store-leftover");
    expect(leftover).toHaveTextContent("still saved as plaintext in /data/secrets/api_keys.toml");
    expect(leftover).toHaveTextContent("not used");
    await user.click(screen.getByRole("button", { name: /Delete or keep it/ }));
    expect(screen.getByTestId("plaintext-key-choice")).toBeInTheDocument();
  });

  it("with a working keychain it offers to move the plaintext key there (the default choice)", async () => {
    const user = userEvent.setup();
    secretStatus = { ...keychainStatus, file_store_has_keys: true };
    renderSection(remoteSettings);

    expect(screen.getByTestId("file-store-leftover")).toHaveTextContent("plaintext key file");
    await user.click(screen.getByRole("button", { name: /Move or delete it/ }));
    expect(screen.getByRole("radio", { name: /Move it into macOS Keychain/ })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Move into macOS Keychain" }));
    expect(setFileStoreMutate).toHaveBeenCalledWith({ allow: false, action: "move" }, expect.anything());
  });

  it("does not look for a key under the old keychain name until asked", async () => {
    const user = userEvent.setup();
    secretStatus = { ...keychainStatus, key_source: "none", legacy_checked: false };
    renderSection(remoteSettings);
    const note = screen.getByTestId("legacy-check");
    expect(note).toHaveTextContent("not looked for automatically");
    await user.click(screen.getByRole("button", { name: "Look for it" }));
    expect(checkLegacyArgs).toContain(true); // the status query was re-run with check_legacy
  });

  it("shows no legacy prompt once the old name was checked, or when a key is already found", () => {
    secretStatus = { ...keychainStatus, key_source: "none", legacy_checked: true };
    const { unmount } = renderSection(remoteSettings);
    expect(screen.queryByTestId("legacy-check")).not.toBeInTheDocument();
    unmount();
    secretStatus = { ...keychainStatus, legacy_checked: false };
    renderSection(remoteSettings);
    expect(screen.queryByTestId("legacy-check")).not.toBeInTheDocument();
  });

  it("disables the plaintext-file toggle and explains when an environment variable controls it", () => {
    secretStatus = {
      ...keychainStatus,
      key_source: "none",
      storage: "file",
      keychain_usable: false,
      keychain_name: "none",
      keychain_detail: "no usable keyring backend is installed or running",
      file_store_enabled: true,
      file_store_env_var: "PFA_SECRETS__ALLOW_FILE_STORE",
      guidance: "Linux: install gnome-keyring",
      warning: "File store: API keys are saved as plaintext in /data/secrets/api_keys.toml.",
    };
    renderSection(remoteSettings);

    expect(screen.getByRole("checkbox", { name: "Keep API keys in a plaintext file" })).toBeDisabled();
    expect(screen.getByTestId("file-store-env-note")).toHaveTextContent("PFA_SECRETS__ALLOW_FILE_STORE");
  });

  it("with the file store on: warns it is plaintext and allows saving", async () => {
    const user = userEvent.setup();
    secretStatus = {
      ...keychainStatus,
      key_source: "none",
      storage: "file",
      keychain_usable: false,
      keychain_name: "none",
      keychain_detail: "no usable keyring backend is installed or running",
      file_store_enabled: true,
      guidance: "Linux: install gnome-keyring",
      warning: "File store: API keys are saved as plaintext in /data/secrets/api_keys.toml.",
    };
    renderSection(remoteSettings);

    expect(screen.getByTestId("file-store-warning")).toHaveTextContent("plaintext");
    expect(screen.getByRole("checkbox", { name: "Keep API keys in a plaintext file" })).toBeChecked();
    await user.type(screen.getByLabelText("New API key"), "sk-secret");
    expect(screen.getByRole("button", { name: "Save key" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Save key" }));
    expect(setApiKeyMutate).toHaveBeenCalled();
  });
});
