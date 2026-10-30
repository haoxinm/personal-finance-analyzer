import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { FileDropZone } from "./file-drop-zone";

function makeFileList(files: File[]): FileList {
  const list = files as unknown as FileList;
  return list;
}

describe("FileDropZone", () => {
  it("passes dropped PDFs to onFiles and filters out non-PDFs", () => {
    const onFiles = vi.fn();
    render(<FileDropZone onFiles={onFiles} />);

    const pdf = new File(["%PDF-1.4"], "statement.pdf", { type: "application/pdf" });
    const txt = new File(["hello"], "notes.txt", { type: "text/plain" });

    fireEvent.drop(screen.getByRole("button"), {
      dataTransfer: { files: makeFileList([pdf, txt]) },
    });

    expect(onFiles).toHaveBeenCalledTimes(1);
    expect(onFiles.mock.calls[0]?.[0]).toEqual([pdf]);
  });

  it("does nothing on drop when disabled", () => {
    const onFiles = vi.fn();
    render(<FileDropZone onFiles={onFiles} disabled />);

    const pdf = new File(["%PDF-1.4"], "statement.pdf", { type: "application/pdf" });
    fireEvent.drop(screen.getByRole("button"), {
      dataTransfer: { files: makeFileList([pdf]) },
    });

    expect(onFiles).not.toHaveBeenCalled();
  });

  it("opens the file picker on click and forwards a selection through the hidden input", () => {
    const onFiles = vi.fn();
    render(<FileDropZone onFiles={onFiles} />);

    const pdf = new File(["%PDF-1.4"], "statement.pdf", { type: "application/pdf" });
    const input = screen.getByLabelText("Choose statement PDFs");
    fireEvent.change(input, { target: { files: makeFileList([pdf]) } });

    expect(onFiles).toHaveBeenCalledWith([pdf]);
  });
});
