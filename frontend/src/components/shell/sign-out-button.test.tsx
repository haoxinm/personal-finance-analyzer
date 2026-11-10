import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { SignOutButton } from "./sign-out-button";

describe("SignOutButton", () => {
  afterEach(() => {
    document.head.querySelectorAll('meta[name="pfa-auth"]').forEach((m) => m.remove());
  });

  it("renders nothing without a login (plain localhost)", () => {
    const { container } = render(<SignOutButton />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a Sign out button when the server is behind the login", () => {
    const meta = document.createElement("meta");
    meta.name = "pfa-auth";
    meta.content = "1";
    document.head.appendChild(meta);
    render(<SignOutButton />);
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });
});
