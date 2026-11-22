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

describe("Dialog stacking", () => {
  it("keeps the background inert until the last stacked dialog closes", () => {
    const root = document.createElement("div");
    root.setAttribute("data-testid", "bg");
    document.body.appendChild(root);
    const first = render(<Dialog aria-label="One" onClose={vi.fn()}><p>one</p></Dialog>);
    expect(root).toHaveAttribute("inert");
    const second = render(<Dialog aria-label="Two" onClose={vi.fn()}><p>two</p></Dialog>);
    const overlays = screen.getAllByTestId("dialog-overlay");
    expect(overlays[0]).toHaveAttribute("inert");
    second.unmount();
    expect(overlays[0]).not.toHaveAttribute("inert");
    expect(root).toHaveAttribute("inert");
    first.unmount();
    expect(root).not.toHaveAttribute("inert");
    expect(root).not.toHaveAttribute("aria-hidden");
    root.remove();
  });
});

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

  it("routes Escape and an overlay press to onRequestClose when given, leaving onClose alone", async () => {
    const onClose = vi.fn();
    const onRequestClose = vi.fn();
    render(
      <Dialog aria-label="Guarded" onClose={onClose} onRequestClose={onRequestClose}>
        <DialogBody>
          <input aria-label="Field" />
        </DialogBody>
      </Dialog>,
    );
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByTestId("dialog-overlay"));
    expect(onRequestClose).toHaveBeenCalledTimes(2);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("skips tabindex=-1, disabled and hidden controls in the Tab trap", async () => {
    render(
      <Dialog aria-label="Trap" onClose={vi.fn()}>
        <DialogBody>
          <input aria-label="First" />
          <button tabIndex={-1}>Skipped</button>
          <button disabled>Disabled</button>
          <div hidden>
            <button>Hidden</button>
          </div>
          <input aria-label="Last" />
        </DialogBody>
      </Dialog>,
    );
    screen.getByLabelText("Last").focus();
    await userEvent.tab();
    expect(screen.getByLabelText("First")).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(screen.getByLabelText("Last")).toHaveFocus();
  });

  it("makes the rest of the app inert while open, except exempt elements, and undoes it", async () => {
    render(
      <>
        <Harness />
        <div data-testid="toasts" data-dialog-exempt="">
          toast
        </div>
      </>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(screen.getByTestId("app")).toHaveAttribute("inert");
    expect(screen.getByTestId("app")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByTestId("toasts")).not.toHaveAttribute("inert");
    await userEvent.keyboard("{Escape}");
    expect(screen.getByTestId("app")).not.toHaveAttribute("inert");
    expect(screen.getByTestId("app")).not.toHaveAttribute("aria-hidden");
  });

  it("compensates for the scrollbar it removes and restores the padding", async () => {
    const original = document.documentElement.clientWidth;
    Object.defineProperty(document.documentElement, "clientWidth", { configurable: true, value: window.innerWidth - 15 });
    document.body.style.paddingRight = "";
    render(<Harness />);
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(document.body.style.paddingRight).toBe("15px");
    await userEvent.keyboard("{Escape}");
    expect(document.body.style.paddingRight).toBe("");
    Object.defineProperty(document.documentElement, "clientWidth", { configurable: true, value: original });
  });
});
