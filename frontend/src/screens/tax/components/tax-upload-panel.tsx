import * as React from "react";
import { Link } from "react-router-dom";

import type { components } from "@/api/client";
import { FileDropZone } from "@/components/primitives/file-drop-zone";
import { UserSwitcher } from "@/components/primitives/user-switcher";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

import {
  ADD_PARAM,
  DOCUMENT_PARAM,
  useUploadTaxDocument,
  type TaxImportResponse,
} from "../hooks";
import {
  TAX_PRIVACY_NOTE,
  errorMessage,
  skippedFromMessages,
} from "../logic";

type User = components["schemas"]["User"];

export interface TaxUploadPanelProps {
  users: readonly User[];
  /** Selected user, or null for "All users". */
  selectedUserId: number | null;
}

interface UploadOutcome {
  name: string;
  status: TaxImportResponse["status"] | "failed";
  message: string;
  /** Extra detail lines: forms skipped as already imported, parser warnings. */
  details: string[];
  /** Set when a document row exists that figures can be added to by hand. */
  documentId?: number;
}

function outcomeFor(file: File, r: TaxImportResponse): UploadOutcome {
  const skipped = skippedFromMessages(r.skipped_from ?? []);
  if (skipped.length === 0 && r.skipped_duplicate_forms > 0) {
    skipped.push(
      `${r.skipped_duplicate_forms} duplicate form${r.skipped_duplicate_forms === 1 ? " was" : "s were"} skipped.`,
    );
  }
  const details = [...skipped, ...r.warnings];
  switch (r.status) {
    case "parsed":
      return {
        name: file.name,
        status: r.status,
        message: `Imported ${r.inserted_forms} form${r.inserted_forms === 1 ? "" : "s"}.`,
        details,
      };
    case "duplicate":
      return {
        name: file.name,
        status: r.status,
        message: "Already imported; nothing changed.",
        details,
      };
    default:
      return {
        name: file.name,
        status: r.status,
        message: r.error ?? "This file could not be read as a tax form.",
        details,
        documentId: r.document.id,
      };
  }
}

/** Upload W-2 / 1099 / 1040 PDFs for one user and list what became of each file. */
export function TaxUploadPanel({ users, selectedUserId }: TaxUploadPanelProps) {
  const upload = useUploadTaxDocument();
  // The drop zone needs one concrete user; with "All users" selected the user picks one here.
  const [uploadUser, setUploadUser] = React.useState<number | null>(null);
  const targetUserId = selectedUserId ?? uploadUser ?? users[0]?.id ?? null;
  const [taxYear, setTaxYear] = React.useState("");
  const [outcomes, setOutcomes] = React.useState<UploadOutcome[]>([]);
  const [uploading, setUploading] = React.useState(false);

  const yearNumber = taxYear.trim() === "" ? undefined : Number(taxYear);
  const yearInvalid =
    yearNumber !== undefined &&
    (!Number.isInteger(yearNumber) || yearNumber < 1990 || yearNumber > 2100);

  async function onFiles(files: File[]) {
    if (targetUserId === null || yearInvalid) return;
    setUploading(true);
    const results: UploadOutcome[] = [];
    for (const file of files) {
      try {
        const r = await upload.mutateAsync({
          file,
          userId: targetUserId,
          taxYear: yearNumber,
        });
        results.push(outcomeFor(file, r));
      } catch (e) {
        results.push({
          name: file.name,
          status: "failed",
          message: errorMessage(e, "Upload failed."),
          details: [],
        });
      }
    }
    setOutcomes(results);
    setUploading(false);
  }

  return (
    <div className="flex flex-col gap-3" data-testid="tax-upload">
      {selectedUserId === null && users.length > 1 ? (
        <label className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground">Upload for</span>
          <UserSwitcher
            users={users}
            value={targetUserId}
            onChange={setUploadUser}
            allowAll={false}
            variant="select"
            aria-label="Upload for user"
          />
        </label>
      ) : null}
      <FileDropZone
        onFiles={(f) => void onFiles(f)}
        disabled={uploading || targetUserId === null || yearInvalid}
        title="Drag tax forms here, or click to browse"
        hint="W-2, 1099 and 1040 PDFs with a text layer. Scanned forms can’t be read."
        inputLabel="Choose tax form PDFs"
      />
      <label className="flex items-center gap-2 text-xs text-muted-foreground">
        Tax year override (only if a form doesn’t print one)
        <Input
          type="number"
          inputMode="numeric"
          min={1990}
          max={2100}
          value={taxYear}
          onChange={(e) => setTaxYear(e.target.value)}
          aria-invalid={yearInvalid}
          className="h-8 w-24"
        />
      </label>
      <p
        className="text-xs text-muted-foreground"
        data-testid="tax-privacy-note"
      >
        {TAX_PRIVACY_NOTE}
      </p>
      {uploading ? (
        <p role="status" className="text-sm">
          Reading tax forms…
        </p>
      ) : null}
      {outcomes.length > 0 ? (
        <ul
          className="flex flex-col gap-1 text-sm"
          data-testid="upload-outcomes"
        >
          {outcomes.map((o, i) => (
            <li
              key={`${o.name}-${i}`}
              className={
                o.status === "parsed" || o.status === "duplicate"
                  ? ""
                  : "text-destructive"
              }
            >
              <span className="font-medium">{o.name}</span>: {o.message}
              {o.documentId !== undefined ? (
                <Button asChild size="sm" variant="outline" className="ml-2">
                  <Link
                    to={`/tax/documents?${DOCUMENT_PARAM}=${o.documentId}&${ADD_PARAM}=1`}
                  >
                    Add figures manually
                  </Link>
                </Button>
              ) : null}
              {o.details.length > 0 ? (
                <ul className="list-disc pl-5 text-xs text-muted-foreground">
                  {o.details.map((t, j) => (
                    <li key={j}>{t}</li>
                  ))}
                </ul>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
