import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Page, PageHeader } from "./page";

describe("PageHeader", () => {
  it("renders the one h1 with its description and actions", () => {
    render(
      <Page>
        <PageHeader title="Tax estimate" description="A planning estimate." actions={<button type="button">Export</button>} />
      </Page>,
    );
    expect(screen.getByRole("heading", { level: 1, name: "Tax estimate" })).toBeInTheDocument();
    expect(screen.getByText("A planning estimate.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export" })).toBeInTheDocument();
  });

  it("omits the description and actions when not given", () => {
    const { container } = render(<PageHeader title="Settings" />);
    expect(container.querySelectorAll("p")).toHaveLength(0);
    expect(container.querySelector("header > div:nth-child(2)")).toBeNull();
  });
});
