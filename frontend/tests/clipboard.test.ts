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

  it("uses the async Clipboard API when available", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard(writeText);

    await copyToClipboard("hello");

    expect(writeText).toHaveBeenCalledWith("hello");
    expect(execSpy).not.toHaveBeenCalled();
  });

  it("falls back to execCommand when the Clipboard API rejects (mobile / insecure context)", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("not allowed"));
    setClipboard(writeText);

    await copyToClipboard("event-id-123");

    expect(writeText).toHaveBeenCalledWith("event-id-123");
    // The fallback created a textarea holding the text and invoked copy.
    const ta = document.querySelector("textarea");
    // The textarea is removed after copy, so it should be gone now.
    expect(ta).toBeNull();
    expect(execSpy).toHaveBeenCalledWith("copy");
  });

  it("falls back to execCommand when navigator.clipboard is undefined", async () => {
    // Simulate an insecure context where the Clipboard API is absent.
    setClipboard(undefined);

    await copyToClipboard("no-clip");

    expect(execSpy).toHaveBeenCalledWith("copy");
  });
});
