import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as React from "react";
import { describe, expect, it, vi } from "vitest";

import { Dialog, DialogBody, DialogFooter, DialogHeader, DialogTitle } from "./dialog";

function Harness({ onClose = vi.fn(), long = false }: { onClose?: () => void; long?: boolean }) {
  const [open, setOpen] = React.useState(false);
  return (
    <div data-testid="app">
      <button onClick={() => setOpen(true)}>Open</button>
      {open ? (
        <Dialog
          aria-label="Test dialog"
          onClose={() => {
            onClose();
            setOpen(false);
          }}
        >
          <DialogHeader>
            <DialogTitle>Title</DialogTitle>
          </DialogHeader>
          <DialogBody data-testid="body">
            <input aria-label="First" />
            {long ? <p data-testid="long">{"x".repeat(5000)}</p> : null}
          </DialogBody>
          <DialogFooter data-testid="footer">
            <button>Cancel</button>
            <button>Save</button>
          </DialogFooter>
        </Dialog>
      ) : null}
    </div>
  );
}

describe("Dialog", () => {
  it("portals to document.body with dialog semantics, above the sticky header", async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    const dialog = screen.getByRole("dialog", { name: "Test dialog" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByTestId("app")).not.toContainElement(dialog);
    expect(document.body).toContainElement(dialog);
    expect(screen.getByTestId("dialog-overlay").className).toContain("z-50");
  });

  it("focuses the first body control and returns focus to the opener", async () => {
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Open" });
    await userEvent.click(opener);
    expect(screen.getByLabelText("First")).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("closes on Escape and on an overlay press, but not on a press inside", async () => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    await userEvent.click(screen.getByLabelText("First"));
    expect(onClose).not.toHaveBeenCalled();
    // A drag that starts inside and ends on the overlay does not close it.
    await userEvent.pointer([
      { keys: "[MouseLeft>]", target: screen.getByLabelText("First") },
      { keys: "[/MouseLeft]", target: screen.getByTestId("dialog-overlay") },
    ]);
    expect(onClose).not.toHaveBeenCalled();
    await userEvent.click(screen.getByTestId("dialog-overlay"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("traps Tab and Shift+Tab", async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    screen.getByRole("button", { name: "Save" }).focus();
    await userEvent.tab();
    expect(screen.getByLabelText("First")).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(screen.getByRole("button", { name: "Save" })).toHaveFocus();
  });

  it("locks page scroll while open and restores it afterwards", async () => {
    document.body.style.overflow = "auto";
    render(<Harness />);
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(document.body.style.overflow).toBe("hidden");
    await userEvent.keyboard("{Escape}");
    expect(document.body.style.overflow).toBe("auto");
    document.body.style.overflow = "";
  });

  it("scrolls the body, keeps the footer outside it, and wraps long unbroken text", async () => {
    render(<Harness long />);
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    const body = screen.getByTestId("body");
    const footer = screen.getByTestId("footer");
    expect(body.className).toContain("overflow-y-auto");
    expect(body.className).toContain("[overflow-wrap:anywhere]");
    expect(body.className).toContain("min-h-0");
    expect(body).not.toContainElement(footer);
    expect(footer.className).toContain("shrink-0");
    expect(screen.getByRole("dialog").className).toContain("max-h-[calc(100dvh-2rem)]");
    expect(screen.getByRole("button", { name: "Save" })).toBeVisible();
  });
});
