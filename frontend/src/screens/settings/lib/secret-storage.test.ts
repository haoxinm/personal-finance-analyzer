import { describe, expect, it } from "vitest";

import { cannotSaveKey, describeKeySource, type SecretStorageStatus } from "./secret-storage";

const base: SecretStorageStatus = {
  key_source: "none",
  storage: "keychain",
  keychain_name: "macOS Keychain",
  keychain_usable: true,
  keychain_detail: "",
  env_var: null,
  key_legacy: false,
  file_store_enabled: false,
  file_store_path: "/data/secrets/api_keys.toml",
  file_store_has_keys: false,
  guidance: "",
  warning: "",
};

describe("describeKeySource", () => {
  it("names the environment variable, keychain or file", () => {
    expect(describeKeySource({ ...base, key_source: "env", env_var: "PFA_LLM_API_KEY" })).toContain("PFA_LLM_API_KEY");
    expect(describeKeySource({ ...base, key_source: "keychain" })).toBe("Key stored in macOS Keychain");
    expect(describeKeySource({ ...base, key_source: "file" })).toContain("plaintext");
  });

  it("says a key under the old keychain name will be moved on next use", () => {
    const text = describeKeySource({ ...base, key_source: "keychain", key_legacy: true });
    expect(text).toContain("old name");
    expect(text).toContain("moved on next use");
  });

  it("says where a new key would go when none is saved", () => {
    expect(describeKeySource(base)).toBe("A saved key will go to macOS Keychain");
    expect(describeKeySource({ ...base, storage: "file", keychain_usable: false })).toContain("plaintext");
    expect(describeKeySource({ ...base, storage: "none", keychain_usable: false })).toContain("No secure place");
  });
});

describe("cannotSaveKey", () => {
  it("is true only with no keychain and the file store off", () => {
    expect(cannotSaveKey(undefined)).toBe(false);
    expect(cannotSaveKey(base)).toBe(false);
    expect(cannotSaveKey({ ...base, storage: "file" })).toBe(false);
    expect(cannotSaveKey({ ...base, storage: "none" })).toBe(true);
  });
});
