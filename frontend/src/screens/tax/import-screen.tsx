import { Card, CardContent } from "@/components/ui/card";
import { useSelectedUser } from "@/hooks/use-selected-user";

import { TaxUploadPanel } from "./components/tax-upload-panel";

export const screenMeta = { path: "/tax/import", title: "Import" };

/**
 * Income & Tax import: upload W-2, 1099 and 1040 PDFs. Each file's outcome is listed here; figures
 * that could not be read are completed by hand on the Documents tab.
 */
export default function TaxImportScreen() {
  const sel = useSelectedUser();

  return (
    <div className="flex flex-col gap-6 py-2">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Import tax forms</h1>
        <p className="text-sm text-muted-foreground">
          Upload W-2, 1099 and 1040 PDFs to add them to your income and tax figures.
        </p>
      </div>
      <Card>
        <CardContent className="pt-4">
          <TaxUploadPanel users={sel.users} selectedUserId={sel.selectedUserId} />
        </CardContent>
      </Card>
    </div>
  );
}
