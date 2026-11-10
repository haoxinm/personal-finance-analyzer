import * as React from "react";

import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/use-toast";

import { useCreateTaxForm, useTaxDocument } from "../hooks";
import {
  FORM_TYPE_OPTIONS,
  blankToNull,
  errorMessage,
  last4Error,
  taxYearError,
  type TaxDocument,
  type TaxFormType,
} from "../logic";
import { SelectField, TextField } from "./fields";
import { FormCard } from "./form-card";

function AddFormEditor({
  document,
  onClose,
}: {
  document: TaxDocument;
  onClose: () => void;
}) {
  const { toast } = useToast();
  const create = useCreateTaxForm(document.user_id);
  const [formType, setFormType] = React.useState<TaxFormType>("w2");
  const [payer, setPayer] = React.useState("");
  const [year, setYear] = React.useState(
    document.tax_year ? String(document.tax_year) : "",
  );
  const [last4, setLast4] = React.useState("");
  const [touched, setTouched] = React.useState(false);
  const yearErr = taxYearError(year);
  const last4Err = last4Error(last4);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (yearErr || last4Err) return;
    try {
      await create.mutateAsync({
        documentId: document.id,
        body: {
          form_type: formType,
          tax_year: Number(year),
          payer_name: blankToNull(payer),
          payer_tin_last4: blankToNull(last4),
          is_corrected: false,
        },
      });
      toast({ variant: "success", title: "Form added" });
      onClose();
    } catch (err) {
      toast({
        variant: "destructive",
        title: "Could not add form",
        description: errorMessage(err),
      });
    }
  }

  return (
    <form
      onSubmit={(e) => void submit(e)}
      aria-label="Add form"
      className="flex flex-wrap items-start gap-2 rounded-md border border-dashed border-border p-3"
    >
      <SelectField
        label="Form type"
        value={formType}
        options={FORM_TYPE_OPTIONS}
        onChange={(v) => v && setFormType(v)}
        className="w-40"
      />
      <TextField
        label="Payer name"
        value={payer}
        maxLength={120}
        onChange={(e) => setPayer(e.target.value)}
        className="w-52"
      />
      <TextField
        label="Payer ID last 4"
        inputMode="numeric"
        maxLength={4}
        value={last4}
        error={touched ? last4Err : null}
        onChange={(e) => setLast4(e.target.value)}
        className="w-24"
      />
      <TextField
        label="Tax year"
        inputMode="numeric"
        value={year}
        error={touched ? yearErr : null}
        onChange={(e) => setYear(e.target.value)}
        className="w-20"
      />
      <span className="flex items-center gap-2 self-end pb-0.5">
        <Button type="submit" size="sm" disabled={create.isPending}>
          Add form
        </Button>
        <Button type="button" size="sm" variant="outline" onClick={onClose}>
          Cancel
        </Button>
      </span>
    </form>
  );
}

/** The forms of one document with their items, editable. Loaded only while expanded. */
export function DocumentDetail({
  document,
  adding,
  onAddingChange,
}: {
  document: TaxDocument;
  adding: boolean;
  onAddingChange: (adding: boolean) => void;
}) {
  const query = useTaxDocument(document.id, document.user_id);
  const forms = query.data?.forms ?? [];
  const noFigures = document.status !== "parsed" && forms.length === 0;

  if (query.isPending)
    return (
      <p role="status" className="text-xs text-muted-foreground">
        Loading figures…
      </p>
    );
  if (query.isError) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <p role="alert" className="text-xs text-destructive">
          {errorMessage(query.error, "Could not load this document.")}
        </p>
        <Button size="sm" variant="outline" onClick={() => void query.refetch()}>
          Try again
        </Button>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3" data-testid="document-detail">
      {noFigures ? (
        <p className="text-xs text-muted-foreground">
          No figures were read. Add them manually.
        </p>
      ) : forms.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          This document has no forms.
        </p>
      ) : null}
      {forms.map((f) => (
        <FormCard
          key={f.id}
          form={f}
          siblings={forms.length - 1}
          ownerId={document.user_id}
        />
      ))}
      {adding ? (
        <AddFormEditor
          document={document}
          onClose={() => onAddingChange(false)}
        />
      ) : (
        <div>
          <Button
            size="sm"
            variant="outline"
            onClick={() => onAddingChange(true)}
          >
            Add form
          </Button>
        </div>
      )}
    </div>
  );
}
