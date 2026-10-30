import { Page, PageHeader } from "@/components/primitives/page";
import { Card, CardContent } from "@/components/ui/card";
import { useSelectedUser } from "@/hooks/use-selected-user";

import { TaxUploadPanel } from "./components/tax-upload-panel";

export const screenMeta = { path: "/tax/import", title: "Import" };

/**
 * Income & tax import: upload W-2, 1099 and 1040 PDFs. Each file's outcome is listed here; figures
 * that could not be read are completed by hand on the Documents tab.
 */
export default function TaxImportScreen() {
  const sel = useSelectedUser();

  return (
    <Page>
      <PageHeader
        title="Import tax forms"
        description="Upload W-2, 1099 and 1040 PDFs to add them to your income and tax figures."
      />
      <Card>
        <CardContent className="pt-4">
          <TaxUploadPanel users={sel.users} selectedUserId={sel.selectedUserId} />
        </CardContent>
      </Card>
    </Page>
  );
}
