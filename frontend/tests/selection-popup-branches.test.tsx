import { act, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";

// Control clipboard.resolve(true|false) per test.
const clipboardMock = vi.hoisted(() => vi.fn());
vi.mock("../src/utils/clipboard", () => ({ copyToClipboard: clipboardMock }));

// Keep isTouchInteractionViewport real (viewport-driven) but capture the
// mobile action sheet so the desktop popup path and the sheet path are both
// observable without rendering the full sheet DOM.
const sheetSpy = vi.hoisted(() => vi.fn());
vi.mock("../src/components/MobileActionSheet", async () => {
  const actual =
    await vi.importActual<typeof import("../src/components/MobileActionSheet")>(
      "../src/components/MobileActionSheet",
    );
  return {
    ...actual,
    useMobileActionSheet: () => ({
      show: sheetSpy,
      dismiss: vi.fn(),
      visible: false,
    }),
  };
});

// tests/setup.ts globally mocks SelectionPopup to () => null; unmock and
// dynamically import the real component (after the module mocks below apply).
vi.unmock("../src/components/SelectionPopup");
const { SelectionPopup } = await import("../src/components/SelectionPopup");

// --- listener capture -------------------------------------------------------

interface CapturedListener {
  type: string;
  fn: EventListener;
}
let captured: CapturedListener[];
let origAdd: typeof document.addEventListener;
let origRemove: typeof document.removeEventListener;

const onAdd = vi.hoisted(() => vi.fn());

beforeEach(() => {
  clipboardMock.mockReset();
  clipboardMock.mockResolvedValue(true);
  sheetSpy.mockReset();
  onAdd.mockReset();
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    value: 1024,
  });
  Object.defineProperty(navigator, "maxTouchPoints", {
    configurable: true,
    value: 0,
  });
  vi.spyOn(window, "matchMedia").mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));

  captured = [];
  origAdd = document.addEventListener.bind(document);
  origRemove = document.removeEventListener.bind(document);
  document.addEventListener = ((
    type: string,
    fn: EventListenerOrEventListenerObject,
    options?: boolean | AddEventListenerOptions,
  ) => {
    if (typeof fn === "function") captured.push({ type, fn });
    return origAdd(type, fn, options);
  }) as typeof document.addEventListener;
});

afterEach(() => {
  vi.restoreAllMocks();
  document.addEventListener = origAdd;
  document.removeEventListener = origRemove;
});

// --- helpers ----------------------------------------------------------------

interface SelOpts {
  text?: string;
  collapsed?: boolean;
  anchor?: Node | null;
  rect?: { left: number; width: number; bottom: number };
}

function stubSel(opts: SelOpts = {}): Selection {
  const s = {
    anchorNode: opts.anchor !== undefined ? opts.anchor : null,
    isCollapsed: opts.collapsed ?? false,
    toString: () => opts.text ?? "",
    getRangeAt: () => ({
      getBoundingClientRect: () => opts.rect ?? { left: 10, width: 100, bottom: 20 },
    }),
    removeAllRanges: vi.fn(),
  } as unknown as Selection;
  vi.spyOn(window, "getSelection").mockReturnValue(s);
  return s;
}

async function waitForListeners(): Promise<void> {
  await waitFor(() => {
    expect(captured.some((c) => c.type === "mouseup")).toBe(true);
    expect(captured.some((c) => c.type === "touchend")).toBe(true);
    expect(captured.some((c) => c.type === "contextmenu")).toBe(true);
  });
}

function fire(type: string, event: Event): void {
  const listener = captured.find((c) => c.type === type)!;
  act(() => {
    listener.fn(event);
  });
}

function mouseUp(target?: Node): void {
  const ev = new MouseEvent("mouseup", { bubbles: true });
  if (target) Object.defineProperty(ev, "target", { value: target });
  fire("mouseup", ev);
}

function touchEnd(target?: Node): void {
  const ev = new TouchEvent("touchend", { bubbles: true });
  if (target) Object.defineProperty(ev, "target", { value: target });
  fire("touchend", ev);
}

const tick = (ms: number) =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });

/** Standard fixture: a message with selectable text plus detached helpers. */
function mountFixture(extra?: React.ReactNode) {
  return render(
    <>
      <div data-message-id="m1">
        <span data-testid="start">alpha special omega</span>
      </div>
      <div data-message-id="">
        <span data-testid="empty-msg">empty</span>
      </div>
      <div data-testid="outside">
        <span data-testid="outspan">outside text</span>
      </div>
      {extra}
      <SelectionPopup onAdd={onAdd} />
    </>,
  );
}

async function openPopup(container: HTMLElement): Promise<HTMLElement> {
  const start = container.querySelector('[data-testid="start"]')!;
  stubSel({ text: "alpha special omega", anchor: start, collapsed: false });
  mouseUp();
  const popup = await waitFor(() =>
    container.querySelector(".selection-popup") as HTMLElement,
  );
  expect(popup).toBeTruthy();
  return popup;
}

// --- tests ------------------------------------------------------------------

describe("SelectionPopup branch coverage", () => {
  it("a click-away with no selection defers a dismiss that tears down the open popup's range", async () => {
    const { container } = mountFixture();
    await waitForListeners();
    await openPopup(container);

    // handleMouseUp reads getSelection three times synchronously: at entry
    // (collapsed → deferred branch), in the timer callback (currentSel still
    // collapsed → dismiss()), then inside dismiss (non-collapsed → teardown).
    let reads = 0;
    const teardown = vi.fn();
    const sel = {
      anchorNode: null,
      get isCollapsed() {
        return [true, true, false][reads++] ?? false;
      },
      toString: () => "",
      removeAllRanges: teardown,
    } as unknown as Selection;
    vi.spyOn(window, "getSelection").mockReturnValue(sel);

    mouseUp();
    await tick(5);

    expect(teardown).toHaveBeenCalled();
    expect(container.querySelector(".selection-popup")).toBeNull();
  });

  it("does not dismiss when a selection re-appears before the deferred check fires", async () => {
    const { container } = mountFixture();
    await waitForListeners();
    await openPopup(container);

    stubSel({ text: "", collapsed: true }); // entry: collapsed → deferred branch
    mouseUp();
    stubSel({ text: "still selected", collapsed: false }); // at fire: selected
    await tick(5);

    expect(container.querySelector(".selection-popup")).not.toBeNull();
  });

  it("ignores a mouseup that originates inside the popup element", async () => {
    const { container } = mountFixture();
    await waitForListeners();
    const popup = await openPopup(container);

    stubSel({ text: "", collapsed: true }); // would normally dismiss
    mouseUp(popup);
    await tick(5);

    expect(container.querySelector(".selection-popup")).not.toBeNull();
  });

  it("skips the synthesized mouseup after touchend, then shows the popup via the touch path", async () => {
    const { container } = mountFixture();
    await waitForListeners();
    const start = container.querySelector('[data-testid="start"]')!;
    stubSel({ text: "alpha special omega", anchor: start, collapsed: false });

    touchEnd(); // arm
    touchEnd(); // re-arm (clears the prior timer)
    mouseUp(); // synthesized mouseup echo-skipped
    expect(container.querySelector(".selection-popup")).toBeNull();

    await tick(450); // touch timer fires; desktop viewport → floating popup
    expect(container.querySelector(".selection-popup")).not.toBeNull();
  });

  it("touchend inside the popup does not arm a selection timer", async () => {
    const { container } = mountFixture();
    await waitForListeners();
    const popup = await openPopup(container);
    const start = container.querySelector('[data-testid="start"]')!;

    stubSel({ text: "alpha special omega", anchor: start, collapsed: false });
    touchEnd(popup);
    await tick(450);

    expect(sheetSpy).not.toHaveBeenCalled();
    expect(container.querySelectorAll(".selection-popup")).toHaveLength(1);
  });

  it("resolves a selection anchored on a text node via its parent element", async () => {
    const { container } = mountFixture();
    await waitForListeners();
    const msg = container.querySelector('[data-message-id="m1"]')!;
    const textNode = document.createTextNode("alpha special omega");
    msg.appendChild(textNode);
    stubSel({ text: "alpha special omega", anchor: textNode, collapsed: false });
    mouseUp();
    await tick(5);
    expect(container.querySelector(".selection-popup")).not.toBeNull();
  });

  it("dismiss leaves a collapsed selection intact while still closing the popup", async () => {
    const { container } = mountFixture();
    await waitForListeners();
    await openPopup(container);

    // Entry + currentSel reads are collapsed (triggers dismiss); the dismiss
    // read is also collapsed, so the range is left untouched.
    let reads = 0;
    const noop = vi.fn();
    const sel = {
      anchorNode: null,
      get isCollapsed() {
        return [true, true, true][reads++] ?? true;
      },
      toString: () => "",
      removeAllRanges: noop,
    } as unknown as Selection;
    vi.spyOn(window, "getSelection").mockReturnValue(sel);

    mouseUp();
    await tick(5);

    expect(noop).not.toHaveBeenCalled();
    expect(container.querySelector(".selection-popup")).toBeNull();
  });

  it("does not open a popup when the selection is not inside a message", async () => {
    const { container } = mountFixture();
    await waitForListeners();
    const outside = container.querySelector('[data-testid="outspan"]')!;
    stubSel({ text: "outside text", anchor: outside, collapsed: false });
    mouseUp();
    await tick(5);
    expect(container.querySelector(".selection-popup")).toBeNull();
  });

  it("does not open a popup when the message has an empty id", async () => {
    const { container } = mountFixture();
    await waitForListeners();
    const emptyMsg = container.querySelector('[data-testid="empty-msg"]')!;
    stubSel({ text: "empty", anchor: emptyMsg, collapsed: false });
    mouseUp();
    await tick(5);
    expect(container.querySelector(".selection-popup")).toBeNull();
  });

  it("touch path with a collapsed selection at fire time opens nothing", async () => {
    const { container } = mountFixture();
    await waitForListeners();
    stubSel({ text: "alpha", collapsed: true });
    touchEnd();
    await tick(450);
    expect(container.querySelector(".selection-popup")).toBeNull();
    expect(sheetSpy).not.toHaveBeenCalled();
  });

  it("contextmenu with no popup open dismisses (a no-op teardown) without touching ranges", async () => {
    const { container } = mountFixture();
    await waitForListeners();
    const teardown = stubSel({ text: "", collapsed: true });
    fire("contextmenu", new MouseEvent("contextmenu", { bubbles: true }));
    expect(teardown.removeAllRanges).not.toHaveBeenCalled();
    expect(container.querySelector(".selection-popup")).toBeNull();
  });

  it("Copy keeps the popup open when copyToClipboard resolves false", async () => {
    const { container } = mountFixture();
    await waitForListeners();
    await openPopup(container);
    clipboardMock.mockResolvedValue(false);

    const copyBtn = container.querySelector(".selection-popup-action-btn")!;
    await act(async () => {
      copyBtn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await tick(5);

    expect(clipboardMock).toHaveBeenCalledWith("alpha special omega");
    expect(container.querySelector(".selection-popup")).not.toBeNull();
  });

  it("Copy closes the popup when copyToClipboard resolves true", async () => {
    const { container } = mountFixture();
    await waitForListeners();
    await openPopup(container);
    clipboardMock.mockResolvedValue(true);

    const [copyBtn] = container.querySelectorAll(".selection-popup-action-btn");
    await act(async () => {
      copyBtn!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    await tick(5);

    expect(container.querySelector(".selection-popup")).toBeNull();
  });

  it("Comment removes ranges, reports the text with an empty comment, and closes", async () => {
    const { container } = mountFixture();
    await waitForListeners();
    await openPopup(container);

    const [, commentBtn] = container.querySelectorAll(".selection-popup-action-btn");
    act(() => {
      commentBtn!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(onAdd).toHaveBeenCalledWith("alpha special omega", "", "m1");
    expect(container.querySelector(".selection-popup")).toBeNull();
  });

  it("mobile touch path opens the action sheet with a truncated label for long text", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    Object.defineProperty(navigator, "maxTouchPoints", { configurable: true, value: 1 });
    vi.mocked(window.matchMedia).mockImplementation((query: string) => ({
      matches: query === "(pointer: coarse)",
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));

    const { container } = mountFixture();
    await waitForListeners();
    const start = container.querySelector('[data-testid="start"]')!;
    const longText = "a".repeat(60);
    stubSel({ text: longText, anchor: start, collapsed: false });

    touchEnd();
    await tick(450);

    expect(sheetSpy).toHaveBeenCalledTimes(1);
    const [, label] = sheetSpy.mock.calls[0];
    expect(label).toBe("a".repeat(40) + "…");
  });

  it("mobile touch path opens the action sheet with the full label for short text", async () => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    Object.defineProperty(navigator, "maxTouchPoints", { configurable: true, value: 1 });
    vi.mocked(window.matchMedia).mockImplementation((query: string) => ({
      matches: query === "(pointer: coarse)",
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));

    const { container } = mountFixture();
    await waitForListeners();
    const start = container.querySelector('[data-testid="start"]')!;
    stubSel({ text: "short", anchor: start, collapsed: false });

    touchEnd();
    await tick(450);

    expect(sheetSpy).toHaveBeenCalledTimes(1);
    const [items, label] = sheetSpy.mock.calls[0];
    expect(label).toBe("short");

    // The sheet's Copy action copies the text; Comment reports it with an
    // empty comment.
    const copy = (items as Array<{ id: string; onClick: () => void }>).find(
      (i) => i.id === "copy",
    )!;
    const comment = (items as Array<{ id: string; onClick: () => void }>).find(
      (i) => i.id === "comment",
    )!;
    await act(async () => {
      await copy.onClick();
    });
    expect(clipboardMock).toHaveBeenCalledWith("short");
    act(() => comment.onClick());
    expect(onAdd).toHaveBeenCalledWith("short", "", "m1");
  });

  it("clears a pending touch selection timer when unmounting", async () => {
    const { container, unmount } = mountFixture();
    await waitForListeners();
    const start = container.querySelector('[data-testid="start"]')!;
    stubSel({ text: "alpha special omega", anchor: start, collapsed: false });

    touchEnd(); // arm the 400ms timer; do NOT let it fire
    expect(() => unmount()).not.toThrow();
  });
});
