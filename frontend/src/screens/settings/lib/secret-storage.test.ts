import { describe, expect, it } from "vitest";

import { cannotSaveKey, describeFileKeyOutcome, describeKeySource, type SecretStorageStatus } from "./secret-storage";

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
  legacy_checked: true,
  file_key_outcome: "none",
  moved_keys: 0,
  kept_keys: 0,
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

describe("describeFileKeyOutcome", () => {
  it("says where the key now lives", () => {
    const moved = describeFileKeyOutcome({ ...base, file_key_outcome: "moved", moved_keys: 1 });
    expect(moved.title).toBe("Moved 1 saved key into macOS Keychain");
    expect(moved.description).toContain("removed");
    expect(describeFileKeyOutcome({ ...base, file_key_outcome: "moved", moved_keys: 2 }).title).toContain("2 saved keys");
    const keptOnly = describeFileKeyOutcome({ ...base, file_key_outcome: "moved", kept_keys: 1 });
    expect(keptOnly.title).toBe("Kept the key already in macOS Keychain");
    expect(keptOnly.description).toContain("not replaced");
    const mixed = describeFileKeyOutcome({ ...base, file_key_outcome: "moved", moved_keys: 1, kept_keys: 1 });
    expect(mixed.title).toBe("Moved 1 saved key into macOS Keychain");
    expect(mixed.description).toContain("kept, not replaced");
    const empty = describeFileKeyOutcome({ ...base, file_key_outcome: "moved" });
    expect(empty.title).toBe("Plaintext key file removed");
    expect(empty.description).toContain("no saved keys");
    const deleted = describeFileKeyOutcome({ ...base, file_key_outcome: "deleted" });
    expect(deleted.title).toContain("deleted");
    expect(deleted.description).toContain("no longer stored");
    const kept = describeFileKeyOutcome({ ...base, file_key_outcome: "kept" });
    expect(kept.description).toContain("/data/secrets/api_keys.toml");
    expect(kept.description).toContain("not used");
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
