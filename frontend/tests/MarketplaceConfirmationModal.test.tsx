// MarketplaceConfirmationModal is a generic confirm modal: focus-trap,
// Escape/overlay/back-button dismiss, busy+disabled overlay neutralization,
// and label defaults. useBackButtonDismiss has its own coverage owner, so it
// is mocked to a spy here — this isolates the modal's own logic AND lets us
// assert the wiring (open, busy||disabled ? noop : onCancel).
vi.mock("../src/hooks/useBackButtonDismiss", () => ({
  useBackButtonDismiss: vi.fn(),
}));

import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { useBackButtonDismiss } from "../src/hooks/useBackButtonDismiss";
import { MarketplaceConfirmationModal } from "../src/components/MarketplaceConfirmationModal";

const noop = () => {};

// i18n runs REAL (setup.ts: empty resources, fallbackLng false -> t(key)
// returns the key verbatim). These are the bare keys the component renders.
const T = {
  cancel: "app.cancel",
  confirm: "app.confirm",
  updating: "settings.extensionsUpdating",
};

function defaultProps(over: Record<string, unknown> = {}) {
  return {
    open: true,
    title: "Confirm install",
    busy: false,
    error: "",
    onConfirm: noop,
    onCancel: noop,
    children: <div data-testid="body">body</div>,
    ...over,
  };
}

afterEach(() => {
  vi.clearAllMocks();
  // activeElement override (focus-capture null-branch test) must not leak.
  if (
    Object.getOwnPropertyDescriptor(document, "activeElement")?.get?.name ===
    "nullActive"
  ) {
    delete (document as Partial<Document>).activeElement;
  }
});

describe("MarketplaceConfirmationModal — open state + wiring", () => {
  it("renders nothing when closed and wires the back-button hook with (false, onCancel)", () => {
    const onCancel = vi.fn();
    const { container } = render(
      <MarketplaceConfirmationModal {...defaultProps({ open: false, onCancel })} />,
    );
    expect(container.firstChild).toBeNull();
    // busy||disabled is false here -> onClose identity is onCancel.
    expect(useBackButtonDismiss).toHaveBeenCalledWith(false, onCancel);
  });

  it("wires the back-button hook with a no-op close when busy", () => {
    render(
      <MarketplaceConfirmationModal {...defaultProps({ open: false, busy: true })} />,
    );
    const [, onClose] = (useBackButtonDismiss as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(useBackButtonDismiss).toHaveBeenCalledWith(false, expect.any(Function));
    // The busy branch passes a no-op, not the real onCancel.
    expect(onClose).not.toBe(noop);
    expect(() => onClose()).not.toThrow();
  });

  it("renders title, eyebrow, error alert, and children when open", () => {
    render(
      <MarketplaceConfirmationModal
        {...defaultProps({ eyebrow: "Marketplace", error: "Something broke" })}
      />,
    );
    expect(screen.getByText("Confirm install")).toBeTruthy();
    expect(screen.getByText("Marketplace")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toBe("Something broke");
    expect(screen.getByTestId("body")).toBeTruthy();
  });

  it("omits eyebrow and error when absent", () => {
    render(<MarketplaceConfirmationModal {...defaultProps()} />);
    expect(document.querySelector(".marketplace-intent-eyebrow")).toBeNull();
    expect(document.querySelector('[role="alert"]')).toBeNull();
  });
});

describe("MarketplaceConfirmationModal — label defaults", () => {
  it("uses app.cancel and app.confirm defaults when no labels given (not busy)", () => {
    render(<MarketplaceConfirmationModal {...defaultProps()} />);
    // cancel button + close (×) aria-label both fall back to app.cancel.
    const cancelBtn = screen.getByText(T.cancel);
    expect(cancelBtn).toBeTruthy();
    expect(screen.getByText(T.confirm)).toBeTruthy();
    expect(screen.getByLabelText(T.cancel)).toBeTruthy();
  });

  it("uses settings.extensionsUpdating for the confirm button when busy", () => {
    render(<MarketplaceConfirmationModal {...defaultProps({ busy: true })} />);
    expect(screen.getByText(T.updating)).toBeTruthy();
  });

  it("custom cancel/confirm labels override the defaults (including close aria-label)", () => {
    render(
      <MarketplaceConfirmationModal
        {...defaultProps({ confirmLabel: "Install now", cancelLabel: "Back out" })}
      />,
    );
    expect(screen.getByText("Install now")).toBeTruthy();
    expect(screen.getByText("Back out")).toBeTruthy();
    expect(screen.getByLabelText("Back out")).toBeTruthy();
  });
});

describe("MarketplaceConfirmationModal — dismiss interactions", () => {
  it("clicking the overlay cancels when not busy/disabled", () => {
    const onCancel = vi.fn();
    const { container } = render(
      <MarketplaceConfirmationModal {...defaultProps({ onCancel })} />,
    );
    fireEvent.click(container.querySelector(".modal-overlay")!);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("clicking inside the content does NOT cancel (stopPropagation)", () => {
    const onCancel = vi.fn();
    const { container } = render(
      <MarketplaceConfirmationModal {...defaultProps({ onCancel })} />,
    );
    fireEvent.click(container.querySelector(".modal-content")!);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("the overlay does not cancel when busy (handler neutralized)", () => {
    const onCancel = vi.fn();
    const { container } = render(
      <MarketplaceConfirmationModal {...defaultProps({ busy: true, onCancel })} />,
    );
    fireEvent.click(container.querySelector(".modal-overlay")!);
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("the close (×), footer cancel, and confirm buttons fire their callbacks", () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    render(
      <MarketplaceConfirmationModal {...defaultProps({ onCancel, onConfirm })} />,
    );
    fireEvent.click(screen.getByText("×"));
    expect(onCancel).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText(T.cancel));
    expect(onCancel).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByText(T.confirm));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("Escape cancels when not busy/disabled", () => {
    const onCancel = vi.fn();
    const { container } = render(
      <MarketplaceConfirmationModal {...defaultProps({ onCancel })} />,
    );
    fireEvent.keyDown(container.querySelector(".modal-content")!, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("Escape does NOT cancel when busy", () => {
    const onCancel = vi.fn();
    const { container } = render(
      <MarketplaceConfirmationModal {...defaultProps({ busy: true, onCancel })} />,
    );
    fireEvent.keyDown(container.querySelector(".modal-content")!, { key: "Escape" });
    expect(onCancel).not.toHaveBeenCalled();
  });
});

describe("MarketplaceConfirmationModal — focus trap", () => {
  // Tab order in the DOM: close (×, header) -> cancel (footer) -> confirm (footer).
  function focusables() {
    return Array.from(
      document.querySelectorAll<HTMLElement>(
        ".modal-content button:not([disabled]), .modal-content [href], .modal-content input:not([disabled]), .modal-content select:not([disabled]), .modal-content textarea:not([disabled]), .modal-content [tabindex]:not([tabindex='-1'])",
      ),
    );
  }

  it("shift+Tab on the first focusable wraps to the last", () => {
    const { container } = render(<MarketplaceConfirmationModal {...defaultProps()} />);
    const f = focusables();
    const first = f[0];
    const last = f[f.length - 1];
    first.focus();
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(container.querySelector(".modal-content")!, {
      key: "Tab",
      shiftKey: true,
    });
    expect(document.activeElement).toBe(last);
  });

  it("Tab on the last focusable wraps to the first", () => {
    const { container } = render(<MarketplaceConfirmationModal {...defaultProps()} />);
    const f = focusables();
    const first = f[0];
    const last = f[f.length - 1];
    last.focus();
    expect(document.activeElement).toBe(last);
    fireEvent.keyDown(container.querySelector(".modal-content")!, { key: "Tab" });
    expect(document.activeElement).toBe(first);
  });

  it("Tab on a middle focusable is a no-op (no wrap)", () => {
    const { container } = render(<MarketplaceConfirmationModal {...defaultProps()} />);
    const f = focusables();
    const middle = f[1];
    middle.focus();
    fireEvent.keyDown(container.querySelector(".modal-content")!, { key: "Tab" });
    expect(document.activeElement).toBe(middle);
  });

  it("non-Tab, non-Escape key is a no-op", () => {
    const onCancel = vi.fn();
    const { container } = render(
      <MarketplaceConfirmationModal {...defaultProps({ onCancel })} />,
    );
    fireEvent.keyDown(container.querySelector(".modal-content")!, { key: "Enter" });
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("Tab is a guarded no-op when there are no focusable controls (busy disables all)", () => {
    const { container } = render(
      <MarketplaceConfirmationModal {...defaultProps({ busy: true })} />,
    );
    expect(focusables()).toHaveLength(0);
    // No wrap target — the handler must return without throwing or focusing.
    fireEvent.keyDown(container.querySelector(".modal-content")!, { key: "Tab" });
    expect(document.activeElement).not.toBe(container.querySelector(".modal-content"));
  });
});

describe("MarketplaceConfirmationModal — focus management", () => {
  it("auto-focuses the cancel button on open and restores the previous element on close", () => {
    const previous = document.body;
    const { unmount } = render(<MarketplaceConfirmationModal {...defaultProps()} />);
    const cancelBtn = screen.getByText(T.cancel);
    expect(document.activeElement).toBe(cancelBtn);
    unmount();
    expect(document.activeElement).toBe(previous);
  });

  it("captures a null previous element without throwing on restore", () => {
    // Force the instanceof HTMLElement branch to false so `previous` is null;
    // the cleanup optional-chains past focus() instead of calling it. The
    // override masks activeElement reads, so only no-throw is assertable.
    Object.defineProperty(document, "activeElement", {
      configurable: true,
      get: function nullActive() {
        return null;
      },
    });
    const { unmount } = render(<MarketplaceConfirmationModal {...defaultProps()} />);
    expect(() => unmount()).not.toThrow();
  });
});

describe("MarketplaceConfirmationModal — confirmDisabled", () => {
  it("confirmDisabled disables only the confirm button; cancel stays enabled", () => {
    const onConfirm = vi.fn();
    render(
      <MarketplaceConfirmationModal
        {...defaultProps({ confirmDisabled: true, onConfirm })}
      />,
    );
    expect((screen.getByText(T.confirm) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByText(T.cancel) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByText(T.confirm));
    expect(onConfirm).not.toHaveBeenCalled();
  });
});

describe("MarketplaceConfirmationModal — disabled", () => {
  it("disabled disables the cancel/close buttons and neutralizes Escape", () => {
    const onCancel = vi.fn();
    const { container } = render(
      <MarketplaceConfirmationModal {...defaultProps({ disabled: true, onCancel })} />,
    );
    expect((screen.getByText(T.cancel) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByText("×") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(container.querySelector(".modal-content")!, { key: "Escape" });
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("disabled neutralizes the overlay click handler", () => {
    const onCancel = vi.fn();
    const { container } = render(
      <MarketplaceConfirmationModal {...defaultProps({ disabled: true, onCancel })} />,
    );
    fireEvent.click(container.querySelector(".modal-overlay")!);
    expect(onCancel).not.toHaveBeenCalled();
  });
});
