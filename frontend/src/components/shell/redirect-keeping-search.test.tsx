import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { RedirectKeepingSearch } from "./redirect-keeping-search";

function Where() {
  const { pathname, search, hash } = useLocation();
  return <div data-testid="where">{pathname + search + hash}</div>;
}

function renderAt(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <Routes>
        <Route path="/old" element={<RedirectKeepingSearch to="/new/place" />} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("RedirectKeepingSearch", () => {
  it("keeps every query param and the hash", () => {
    renderAt("/old?user=2&compare=1&statement_id=42&tab=taxonomy#row-3");
    expect(screen.getByTestId("where")).toHaveTextContent("/new/place?user=2&compare=1&statement_id=42&tab=taxonomy#row-3");
  });

  it("redirects a bare path without inventing a query", () => {
    renderAt("/old");
    expect(screen.getByTestId("where").textContent).toBe("/new/place");
  });
});
