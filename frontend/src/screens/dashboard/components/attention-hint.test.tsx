import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { AttentionHint } from "./attention-hint";

function renderHint(statements: number, folderProblems: number) {
  return render(
    <MemoryRouter>
      <AttentionHint attention={{ statements, folderProblems }} />
    </MemoryRouter>,
  );
}

describe("AttentionHint", () => {
  it("renders nothing when everything is fine", () => {
    const { container } = renderHint(0, 0);
    expect(container).toBeEmptyDOMElement();
  });

  it("links statements to the imported-statements card, singular and plural", () => {
    const { unmount } = renderHint(1, 0);
    expect(screen.getByRole("link", { name: "1 statement needs attention" })).toHaveAttribute(
      "href",
      "/spending/import#imported-statements",
    );
    expect(screen.queryByRole("link", { name: /folder/ })).not.toBeInTheDocument();
    unmount();
    renderHint(3, 0);
    expect(screen.getByRole("link", { name: "3 statements need attention" })).toBeInTheDocument();
  });

  it("links folder problems to the folder-imports card", () => {
    renderHint(0, 2);
    expect(screen.getByRole("link", { name: "2 folder files could not be imported" })).toHaveAttribute(
      "href",
      "/spending/import#folder-imports",
    );
    expect(screen.queryByRole("link", { name: /statement/ })).not.toBeInTheDocument();
  });

  it("shows both links when both apply", () => {
    renderHint(1, 1);
    expect(screen.getAllByRole("link")).toHaveLength(2);
    expect(screen.getByTestId("attention-hint")).toHaveTextContent("totals may be off");
  });
});
