import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { copyToClipboard } from "../src/utils/clipboard";

/**
 * Regression: on mobile (and insecure HTTP contexts) the async Clipboard
 * API rejects or is undefined. Copy actions that only called
 * `navigator.clipboard.writeText(...).catch(() => {})` silently did
 * nothing. The shared util MUST fall back to a textarea +
 * execCommand("copy") so the copy still succeeds.
 */

describe("copyToClipboard fallback", () => {
  let originalClipboard: PropertyDescriptor | undefined;
  let execSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    // happy-dom ships no execCommand; install a stub so spyOn has a target.
    document.execCommand = vi.fn().mockReturnValue(true);
    execSpy = vi.spyOn(document, "execCommand");
  });

  afterEach(() => {
    execSpy.mockRestore();
    // @ts-expect-error cleanup stub
    delete (document as Record<string, unknown>).execCommand;
    if (originalClipboard) {
      Object.defineProperty(navigator, "clipboard", originalClipboard);
      originalClipboard = undefined;
    } else {
      // @ts-expect-error restore by deletion
      delete (navigator as unknown as Record<string, unknown>).clipboard;
    }
  });

  function setClipboard(writeText: unknown) {
    originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
  }

  function selectTextAndFocusControl() {
    const text = document.createTextNode("selected chat text");
    const container = document.createElement("div");
    const button = document.createElement("button");
    container.appendChild(text);
    document.body.append(container, button);
    const range = document.createRange();
    range.selectNodeContents(text);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
    button.focus();
    return {
      button,
      text,
      cleanup: () => {
        selection.removeAllRanges();
        container.remove();
        button.remove();
      },
    };
  }

  it("uses the async Clipboard API when available", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard(writeText);

    await expect(copyToClipboard("hello")).resolves.toBe(true);

    expect(writeText).toHaveBeenCalledWith("hello");
    expect(execSpy).not.toHaveBeenCalled();
  });

  it("falls back to execCommand when the Clipboard API rejects (mobile / insecure context)", async () => {
    let rejectClipboard = (_error: Error) => {};
    const writeText = vi.fn().mockImplementation(
      () => new Promise<void>((_resolve, reject) => {
        rejectClipboard = reject;
      }),
    );
    setClipboard(writeText);
    const { button, text, cleanup } = selectTextAndFocusControl();

    try {
      const copy = copyToClipboard("event-id-123");
      text.data = "newer selected chat text";
      const newerRange = document.createRange();
      newerRange.selectNodeContents(text);
      window.getSelection()!.removeAllRanges();
      window.getSelection()!.addRange(newerRange);
      rejectClipboard(new Error("not allowed"));
      await expect(copy).resolves.toBe(true);

      expect(writeText).toHaveBeenCalledWith("event-id-123");
      expect(document.querySelector("textarea")).toBeNull();
      expect(execSpy).toHaveBeenCalledWith("copy");
      expect(window.getSelection()?.toString()).toBe("newer selected chat text");
      expect(document.activeElement).toBe(button);
    } finally {
      cleanup();
    }
  });

  it("falls back to execCommand when navigator.clipboard is undefined", async () => {
    // Simulate an insecure context where the Clipboard API is absent.
    setClipboard(undefined);

    await expect(copyToClipboard("no-clip")).resolves.toBe(true);

    expect(execSpy).toHaveBeenCalledWith("copy");
  });

  it("reports failure when neither clipboard path succeeds", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("not allowed"));
    setClipboard(writeText);
    execSpy.mockReturnValue(false);
    const { button, cleanup } = selectTextAndFocusControl();

    try {
      await expect(copyToClipboard("blocked")).resolves.toBe(false);
      expect(window.getSelection()?.toString()).toBe("selected chat text");
      expect(document.activeElement).toBe(button);
    } finally {
      cleanup();
    }
  });
});
