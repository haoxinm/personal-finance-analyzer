import type { components } from "@/api/client";

export type SecretStorageStatus = components["schemas"]["SecretStorageStatus"];

/** One line saying where the endpoint's key comes from, or where a newly saved one would go. */
export function describeKeySource(status: SecretStorageStatus): string {
  switch (status.key_source) {
    case "env":
      return `Key supplied by the ${status.env_var ?? "PFA_LLM_API_KEY"} environment variable`;
    case "keychain":
      return `Key stored in ${status.keychain_name}`;
    case "file":
      return "Key stored in the plaintext key file";
    default:
      break;
  }
  if (status.storage === "keychain") return `A saved key will go to ${status.keychain_name}`;
  if (status.storage === "file") return "A saved key will go to the plaintext key file";
  return "No secure place to save a key on this machine";
}

/** True when saving a key from the UI would fail: no keychain and the file store is off. */
export function cannotSaveKey(status: SecretStorageStatus | undefined): boolean {
  return status !== undefined && status.storage === "none";
}
