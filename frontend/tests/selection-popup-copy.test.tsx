import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import React from "react";
import "../src/i18n";

vi.unmock("../src/components/SelectionPopup");
const { SelectionPopup } = await import("../src/components/SelectionPopup");

afterEach(() => {
  vi.restoreAllMocks();
});

interface CapturedListener {
  type: string;
  fn: EventListener;
}

describe("SelectionPopup copy", () => {
  it("copies the captured message selection instead of native copy while the range is still active", async () => {
    const captured: CapturedListener[] = [];
    const originalAddEventListener = document.addEventListener.bind(document);
    const originalRemoveEventListener = document.removeEventListener.bind(document);
    document.addEventListener = ((
      type: string,
      fn: EventListenerOrEventListenerObject,
      options?: boolean | AddEventListenerOptions,
    ) => {
      if (typeof fn === "function") captured.push({ type, fn });
      return originalAddEventListener(type, fn, options);
    }) as typeof document.addEventListener;

    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    let unmount = () => {};
    try {
      ({ unmount } = render(
        <>
          <div data-message-id="m1">
            <span data-testid="start">alpha </span>
            <code>special</code>
            <span data-testid="end"> omega</span>
          </div>
          <SelectionPopup onAdd={() => {}} />
        </>,
      ));

      const start = screen.getByTestId("start");
      const selection = {
        anchorNode: start,
        isCollapsed: false,
        toString: () => "alpha special omega",
        getRangeAt: () => ({
          getBoundingClientRect: () => ({
            left: 10,
            width: 120,
            bottom: 24,
          }),
        }),
        removeAllRanges: vi.fn(),
      } as unknown as Selection;
      vi.spyOn(window, "getSelection").mockReturnValue(selection);

      await waitFor(() => {
        expect(captured.some((listener) => listener.type === "mouseup")).toBe(true);
        expect(captured.some((listener) => listener.type === "keydown")).toBe(true);
      });
      await act(async () => {
        captured.find((listener) => listener.type === "mouseup")!.fn(
          new MouseEvent("mouseup", { bubbles: true }),
        );
      });
      await screen.findByText("Copy");

      expect(window.getSelection()?.isCollapsed).toBe(false);
      await act(async () => {
        captured.find((listener) => listener.type === "keydown")?.fn(
          new KeyboardEvent("keydown", { key: "c", ctrlKey: true }),
        );
      });

      await waitFor(() => {
        expect(writeText).toHaveBeenCalledWith("alpha special omega");
      });
      expect(writeText).toHaveBeenCalledTimes(1);
    } finally {
      unmount();
      document.addEventListener = originalAddEventListener;
      document.removeEventListener = originalRemoveEventListener;
    }
  });
});
