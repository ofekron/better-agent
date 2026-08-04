import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import i18n from "i18next";
import { RewindPopover } from "../src/components/RewindPopover";

/**
 * RewindPopover unit slice. Pure presentation + interaction logic: a
 * two-click confirm state machine, outside-click/Escape dismissal, i18n
 * label + title swaps, and fixed positioning. useBackButtonDismiss is
 * mocked as a no-op (onClose is exercised directly via mousedown/Escape);
 * i18n runs real through the setup.ts instance with the `rewind.*` bundle
 * added here so label/title assertions check formatted text, not bare keys.
 */

const REWIND_EN: Record<string, string> = {
  "rewind.noCheckpoint": "No checkpoint captured",
  "rewind.clickConfirm": "Click again to confirm",
  "rewind.rewindWithFiles": "Rewind with files",
};

// setup.ts globally stubs RewindPopover to `() => null` (it's only reached
// through App). Override that here with the REAL implementation so this
// slice covers the component itself. A test file's own vi.mock wins.
vi.mock("../src/components/RewindPopover", async () => {
  const actual = await vi.importActual<typeof import("../src/components/RewindPopover")>(
    "../src/components/RewindPopover",
  );
  return { RewindPopover: actual.RewindPopover };
});

vi.mock("../src/hooks/useBackButtonDismiss", () => ({
  useBackButtonDismiss: () => {},
}));

beforeEach(() => {
  i18n.addResourceBundle("en", "translation", REWIND_EN, true, true);
});

describe("RewindPopover", () => {
  it("renders at the given position; when disabled, shows the no-checkpoint title and a disabled button", () => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    const { container } = render(
      <RewindPopover x={120} y={80} enabled={false} onConfirm={onConfirm} onClose={onClose} />,
    );
    const btn = container.querySelector(".rewind-popover-button") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.title).toBe("No checkpoint captured");
    expect(btn.textContent).toBe("Rewind with files");

    const root = container.querySelector(".rewind-popover") as HTMLElement;
    expect(root.style.top).toBe("80px");
    expect(root.style.left).toBe("120px");
    expect(root.style.zIndex).toBe("1000");

    // React suppresses onClick on a disabled button, so the `if (!enabled)
    // return` guard in handleClick is unreachable through the rendered UI
    // (the disabled attr is the real protection). Clicking must be a no-op.
    fireEvent.click(btn);
    expect(onConfirm).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("first click arms the confirm state and swaps the label, without confirming or closing", () => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    const { container } = render(
      <RewindPopover x={0} y={0} enabled onConfirm={onConfirm} onClose={onClose} />,
    );
    const btn = container.querySelector(".rewind-popover-button") as HTMLButtonElement;

    fireEvent.click(btn);
    // Click only arms confirm; dismissal listens for mousedown/Escape, not click.
    expect(onClose).not.toHaveBeenCalled();
    expect(onConfirm).not.toHaveBeenCalled();
    expect(btn.textContent).toBe("Click again to confirm");
  });

  it("second click fires onConfirm exactly once", () => {
    const onConfirm = vi.fn();
    const { container } = render(
      <RewindPopover x={0} y={0} enabled onConfirm={onConfirm} onClose={vi.fn()} />,
    );
    const btn = container.querySelector(".rewind-popover-button") as HTMLButtonElement;
    fireEvent.click(btn);
    fireEvent.click(btn);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("a mousedown outside the popover closes it", () => {
    const onClose = vi.fn();
    render(<RewindPopover x={0} y={0} enabled onConfirm={vi.fn()} onClose={onClose} />);
    fireEvent.mouseDown(document.body);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("a mousedown inside the popover does not close it", () => {
    const onClose = vi.fn();
    const { container } = render(
      <RewindPopover x={0} y={0} enabled onConfirm={vi.fn()} onClose={onClose} />,
    );
    const root = container.querySelector(".rewind-popover") as HTMLElement;
    fireEvent.mouseDown(root);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("Escape closes the popover", () => {
    const onClose = vi.fn();
    render(<RewindPopover x={0} y={0} enabled onConfirm={vi.fn()} onClose={onClose} />);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("a non-Escape key does not close the popover", () => {
    const onClose = vi.fn();
    render(<RewindPopover x={0} y={0} enabled onConfirm={vi.fn()} onClose={onClose} />);
    fireEvent.keyDown(document, { key: "Enter" });
    expect(onClose).not.toHaveBeenCalled();
  });
});
