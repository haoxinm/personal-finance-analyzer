import type { components } from "@/api/client";

export type SecretStorageStatus = components["schemas"]["SecretStorageStatus"];

/** One line saying where the endpoint's key comes from, or where a newly saved one would go. */
export function describeKeySource(status: SecretStorageStatus): string {
  switch (status.key_source) {
    case "env":
      return `Key supplied by the ${status.env_var ?? "PFA_LLM_API_KEY"} environment variable`;
    case "keychain":
      return status.key_legacy
        ? `Key stored in ${status.keychain_name} under the old name (spend-analyzer); it will be moved on next use`
        : `Key stored in ${status.keychain_name}`;
    case "file":
      return "Key stored in the plaintext key file";
    default:
      break;
  }
  if (status.storage === "keychain") return `A saved key will go to ${status.keychain_name}`;
  if (status.storage === "file") return "A saved key will go to the plaintext key file";
  return "No secure place to save a key on this machine";
}

/** The toast that says where the key now lives after turning the plaintext key file off. */
export function describeFileKeyOutcome(status: SecretStorageStatus): { title: string; description: string } {
  switch (status.file_key_outcome) {
    case "moved": {
      const moved = status.moved_keys;
      const kept = status.kept_keys;
      const removed = "The plaintext key file was overwritten and removed.";
      if (moved === 0 && kept === 0) {
        return { title: "Plaintext key file removed", description: `It held no saved keys. ${removed}` };
      }
      if (moved === 0) {
        return {
          title: `Kept the key already in ${status.keychain_name}`,
          description: `The saved key was not replaced by the one in the file. ${removed}`,
        };
      }
      const title = `Moved ${moved} saved key${moved === 1 ? "" : "s"} into ${status.keychain_name}`;
      return {
        title,
        description:
          kept > 0
            ? `${kept} already in ${status.keychain_name} ${kept === 1 ? "was" : "were"} kept, not replaced. ${removed}`
            : removed,
      };
    }
    case "deleted":
      return {
        title: "Plaintext key file deleted",
        description: "The key is no longer stored on this computer. Enter it again to save a new one.",
      };
    case "kept":
      return {
        title: "Plaintext key file kept",
        description: `The key stays as plaintext in ${status.file_store_path} and is not used. Delete the file when you no longer need it.`,
      };
    default:
      return { title: "Plaintext key file turned off", description: "" };
  }
}

/** True when saving a key from the UI would fail: no keychain and the file store is off. */
export function cannotSaveKey(status: SecretStorageStatus | undefined): boolean {
  return status !== undefined && status.storage === "none";
}
