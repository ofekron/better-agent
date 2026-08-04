// ConfirmModal is a two-button (confirm/cancel) modal with an overlay-dismiss
// path. useBackButtonDismiss has its own test; mock it to a no-op here so this
// suite isolates ConfirmModal's own render + click logic.
vi.mock("../src/hooks/useBackButtonDismiss", () => ({
  useBackButtonDismiss: () => {},
}));

import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ConfirmModal } from "../src/components/ConfirmModal";

const baseProps = {
  open: true,
  title: "Delete session?",
  message: "This cannot be undone.",
  onConfirm: vi.fn(),
  onCancel: vi.fn(),
};

function rerender(open: boolean) {
  return render(<ConfirmModal {...baseProps} open={open} />);
}

describe("ConfirmModal", () => {
  it("renders nothing when closed", () => {
    const { container } = rerender(false);
    expect(container.innerHTML).toBe("");
  });

  it("renders title and message when open", () => {
    render(<ConfirmModal {...baseProps} />);
    expect(screen.getByText("Delete session?")).toBeTruthy();
    expect(screen.getByText("This cannot be undone.")).toBeTruthy();
  });

  it("uses custom confirm/cancel labels when provided", () => {
    render(
      <ConfirmModal
        {...baseProps}
        confirmLabel="Wipe it"
        cancelLabel="Keep it"
      />,
    );
    expect(screen.getByText("Wipe it")).toBeTruthy();
    expect(screen.getByText("Keep it")).toBeTruthy();
  });

  it("falls back to i18n keys when labels are omitted (setup inits resources empty)", () => {
    render(<ConfirmModal {...baseProps} />);
    // i18next returns the key itself when no resource is loaded.
    expect(screen.getByText("app.cancel")).toBeTruthy();
    expect(screen.getByText("app.confirm")).toBeTruthy();
  });

  it("fires onConfirm and not onCancel when the confirm button is clicked", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<ConfirmModal {...baseProps} onConfirm={onConfirm} onCancel={onCancel} />);
    fireEvent.click(screen.getByText("app.confirm"));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("fires onCancel and not onConfirm when the cancel button is clicked", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<ConfirmModal {...baseProps} onConfirm={onConfirm} onCancel={onCancel} />);
    fireEvent.click(screen.getByText("app.cancel"));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("applies btn-danger class by default and btn-primary when danger=false", () => {
    const { rerender } = render(<ConfirmModal {...baseProps} />);
    expect(screen.getByText("app.confirm").className).toBe("btn-danger");

    rerender(<ConfirmModal {...baseProps} danger={false} />);
    expect(screen.getByText("app.confirm").className).toBe("btn-primary");
  });

  it("dismiss defaults to onCancel (overlay + close button both call it)", () => {
    const onCancel = vi.fn();
    const { container } = render(
      <ConfirmModal {...baseProps} onCancel={onCancel} />,
    );
    // Overlay click → dismiss → onCancel.
    fireEvent.click(container.querySelector(".modal-overlay")!);
    expect(onCancel).toHaveBeenCalledTimes(1);
    // Close (×) button → dismiss → onCancel.
    fireEvent.click(screen.getByText("×"));
    expect(onCancel).toHaveBeenCalledTimes(2);
  });

  it("uses onDismiss separately when provided, leaving onCancel untouched on dismiss", () => {
    const onCancel = vi.fn();
    const onDismiss = vi.fn();
    const { container } = render(
      <ConfirmModal
        {...baseProps}
        onCancel={onCancel}
        onDismiss={onDismiss}
      />,
    );
    fireEvent.click(container.querySelector(".modal-overlay")!);
    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
    // The explicit cancel button still routes to onCancel, not onDismiss.
    fireEvent.click(screen.getByText("app.cancel"));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("does not dismiss when clicking inside the modal content (stopPropagation)", () => {
    const onCancel = vi.fn();
    const { container } = render(
      <ConfirmModal {...baseProps} onCancel={onCancel} />,
    );
    fireEvent.click(container.querySelector(".modal-content")!);
    expect(onCancel).not.toHaveBeenCalled();
  });
});
