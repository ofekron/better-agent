import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

// useBackButtonDismiss wires the browser/Android back button via history
// pushState/popstate — a separate concern with its own coverage. Passthrough
// here so the popover test stays isolated from history side effects.
vi.mock("../src/hooks/useBackButtonDismiss", () => ({
  useBackButtonDismiss: () => {},
}));

import { NewFolderDropPopover } from "../src/components/NewFolderDropPopover";
import type { PopoverAnchor } from "../src/components/SessionTagPopover";

const ANCHOR: PopoverAnchor = { top: 100, bottom: 130, left: 50 };

function setViewport(vw: number, vh: number) {
  Object.defineProperty(window, "innerWidth", { value: vw, configurable: true, writable: true });
  Object.defineProperty(window, "innerHeight", { value: vh, configurable: true, writable: true });
}

/** Mock every element's getBoundingClientRect to a fixed size so the popover's
 *  useLayoutEffect positioning math is deterministic. */
function mockRectSize(width: number, height: number) {
  return vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    left: 0,
    top: 0,
    right: width,
    bottom: height,
    width,
    height,
    x: 0,
    y: 0,
    toJSON() {},
  } as DOMRect);
}

function renderPopover(overrides: { anchor?: PopoverAnchor; onCreate?: (n: string) => void; onClose?: () => void } = {}) {
  const onCreate = overrides.onCreate ?? vi.fn();
  const onClose = overrides.onClose ?? vi.fn();
  const utils = render(
    <NewFolderDropPopover
      anchor={overrides.anchor ?? ANCHOR}
      onCreate={onCreate}
      onClose={onClose}
    />,
  );
  return { ...utils, onCreate, onClose };
}

describe("NewFolderDropPopover — initial render", () => {
  beforeEach(() => setViewport(1024, 768));

  it("renders the dialog, focuses the input, and disables create while empty", () => {
    renderPopover();
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-label")).toBe("session.newFolder");
    const input = screen.getByRole("textbox") as HTMLInputElement;
    expect(input.placeholder).toBe("session.newFolderPrompt");
    expect(document.activeElement).toBe(input);
    const create = screen.getByText("session.createFolder").closest("button") as HTMLButtonElement;
    expect(create.disabled).toBe(true);
  });

  it("enables create once a non-empty name is typed", () => {
    renderPopover();
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "Reports" } });
    const create = screen.getByText("session.createFolder").closest("button") as HTMLButtonElement;
    expect(create.disabled).toBe(false);
  });
});

describe("NewFolderDropPopover — positioning", () => {
  afterEach(() => vi.restoreAllMocks());

  it("places the popover just below the anchor when it fits", () => {
    setViewport(1024, 768);
    mockRectSize(200, 80); // GAP=4, MARGIN=8
    const { container } = renderPopover();
    const dialog = container.querySelector(".session-tag-popover") as HTMLElement;
    expect(dialog.style.top).toBe("134px"); // anchor.bottom(130) + GAP(4)
    expect(dialog.style.left).toBe("50px"); // anchor.left
  });

  it("shifts left to keep the popover on screen when it would overflow the right edge", () => {
    setViewport(1000, 768);
    mockRectSize(200, 80);
    renderPopover({ anchor: { top: 100, bottom: 130, left: 900 } });
    const dialog = screen.getByRole("dialog");
    // 900 + 200 = 1100 > 1000 - 8 → left = 1000 - 200 - 8 = 792
    expect(dialog.style.left).toBe("792px");
  });

  it("clamps left to the margin when the popover is wider than the viewport", () => {
    setViewport(300, 768);
    mockRectSize(400, 80); // wider than viewport → right clamp goes negative
    renderPopover({ anchor: { top: 100, bottom: 130, left: 50 } });
    const dialog = screen.getByRole("dialog");
    // right clamp: 300 - 400 - 8 = -108 → then clamped up to MARGIN(8)
    expect(dialog.style.left).toBe("8px");
  });

  it("flips above the anchor when it would overflow the bottom edge", () => {
    setViewport(1024, 200);
    mockRectSize(200, 80);
    renderPopover({ anchor: { top: 100, bottom: 130, left: 50 } });
    const dialog = screen.getByRole("dialog");
    // top=134; 134 + 80 = 214 > 200 - 8 → top = max(8, 100 - 80 - 4) = 16
    expect(dialog.style.top).toBe("16px");
  });

  it("clamps the flipped-up position to the margin when there is no room above either", () => {
    setViewport(1024, 150);
    mockRectSize(200, 200);
    renderPopover({ anchor: { top: 50, bottom: 80, left: 50 } });
    const dialog = screen.getByRole("dialog");
    // top=84; 84 + 200 > 142 → top = max(8, 50 - 200 - 4) = max(8, -154) = 8
    expect(dialog.style.top).toBe("8px");
  });
});

describe("NewFolderDropPopover — submit", () => {
  beforeEach(() => setViewport(1024, 768));

  it("trims the name and invokes onCreate + onClose on Enter", () => {
    const onCreate = vi.fn();
    const onClose = vi.fn();
    renderPopover({ onCreate, onClose });
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "  My Folder  " } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onCreate).toHaveBeenCalledWith("My Folder");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("prevents default on Enter even when the name is empty, but does not submit", () => {
    const onCreate = vi.fn();
    const onClose = vi.fn();
    renderPopover({ onCreate, onClose });
    const input = screen.getByRole("textbox");
    // fireEvent's own event swallows a `preventDefault` init override, so
    // dispatch a real KeyboardEvent and spy its preventDefault directly.
    const evt = new KeyboardEvent("keydown", { key: "Enter", bubbles: true });
    const preventDefault = vi.spyOn(evt, "preventDefault");
    input.dispatchEvent(evt);
    expect(preventDefault).toHaveBeenCalledTimes(1);
    expect(onCreate).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("submits via the create button click", () => {
    const onCreate = vi.fn();
    const onClose = vi.fn();
    renderPopover({ onCreate, onClose });
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Misc" } });
    fireEvent.click(screen.getByText("session.createFolder").closest("button")!);
    expect(onCreate).toHaveBeenCalledWith("Misc");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps create disabled for whitespace-only names", () => {
    renderPopover();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "    " } });
    const create = screen.getByText("session.createFolder").closest("button") as HTMLButtonElement;
    expect(create.disabled).toBe(true);
  });

  it("ignores non-Enter keys pressed in the input (no submit)", () => {
    const onCreate = vi.fn();
    renderPopover({ onCreate });
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "Misc" } });
    fireEvent.keyDown(input, { key: "a" });
    fireEvent.keyDown(input, { key: "Tab" });
    expect(onCreate).not.toHaveBeenCalled();
  });
});

describe("NewFolderDropPopover — closing", () => {
  beforeEach(() => setViewport(1024, 768));

  it("closes on a mousedown outside the popover", () => {
    const onClose = vi.fn();
    renderPopover({ onClose });
    fireEvent.mouseDown(document.body);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does not close on a mousedown inside the popover", () => {
    const onClose = vi.fn();
    renderPopover({ onClose });
    fireEvent.mouseDown(screen.getByRole("dialog"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("closes on Escape", () => {
    const onClose = vi.fn();
    renderPopover({ onClose });
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("ignores keys other than Escape", () => {
    const onClose = vi.fn();
    renderPopover({ onClose });
    fireEvent.keyDown(document, { key: "a" });
    fireEvent.keyDown(document, { key: "Enter" });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("removes its document listeners after unmount", () => {
    const onClose = vi.fn();
    const { unmount } = renderPopover({ onClose });
    unmount();
    fireEvent.mouseDown(document.body);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });
});
