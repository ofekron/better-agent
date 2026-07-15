import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import React from "react";
import "../src/i18n";

vi.unmock("../src/components/SelectionPopup");
const { SelectionPopup } = await import("../src/components/SelectionPopup");
const { MobileActionSheetProvider } = await import("../src/components/MobileActionSheet");

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    value: 1024,
  });
  Object.defineProperty(navigator, "maxTouchPoints", {
    configurable: true,
    value: 0,
  });
});

interface CapturedListener {
  type: string;
  fn: EventListener;
}

describe("SelectionPopup copy", () => {
  function captureDocumentListeners() {
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

    return {
      captured,
      restore: () => {
        document.addEventListener = originalAddEventListener;
        document.removeEventListener = originalRemoveEventListener;
      },
    };
  }

  function stubSelectedMessageText(text: string) {
    const start = screen.getByTestId("start");
    const selection = {
      anchorNode: start,
      isCollapsed: false,
      toString: () => text,
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
    return selection;
  }

  it("copies the captured message selection instead of native copy while the range is still active", async () => {
    const { captured, restore } = captureDocumentListeners();

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

      stubSelectedMessageText("alpha special omega");

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
      restore();
    }
  });

  it("opens the mobile copy sheet for touch selection on wide Android-style viewports", async () => {
    const { captured, restore } = captureDocumentListeners();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 720,
    });
    Object.defineProperty(navigator, "maxTouchPoints", {
      configurable: true,
      value: 1,
    });
    vi.stubGlobal("matchMedia", vi.fn().mockImplementation((query: string) => ({
      matches: query === "(pointer: coarse)",
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })));

    let unmount = () => {};
    try {
      ({ unmount } = render(
        <MobileActionSheetProvider>
          <div data-message-id="m1">
            <span data-testid="start">alpha </span>
            <code>special</code>
            <span data-testid="end"> omega</span>
          </div>
          <SelectionPopup onAdd={() => {}} />
        </MobileActionSheetProvider>,
      ));

      stubSelectedMessageText("alpha special omega");

      await waitFor(() => {
        expect(captured.some((listener) => listener.type === "touchend")).toBe(true);
      });
      // Fake setTimeout so the 400ms touch-selection delay advances instantly.
      vi.useFakeTimers({
        shouldAdvanceTime: true,
        advanceTimeDelta: 1,
        toFake: ["setTimeout", "clearTimeout"],
      });
      act(() => {
        captured.find((listener) => listener.type === "touchend")!.fn(
          new TouchEvent("touchend", { bubbles: true }),
        );
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(450);
      });
      vi.useRealTimers();

      await screen.findByText("Cancel");
      await act(async () => {
        screen.getByRole("button", { name: "Copy" }).click();
      });

      await waitFor(() => {
        expect(writeText).toHaveBeenCalledWith("alpha special omega");
      });
    } finally {
      unmount();
      restore();
    }
  });

  it("keeps mouse selection on the desktop popup even when touch hardware exists", async () => {
    const { captured, restore } = captureDocumentListeners();
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 720,
    });
    Object.defineProperty(navigator, "maxTouchPoints", {
      configurable: true,
      value: 1,
    });

    let unmount = () => {};
    try {
      ({ unmount } = render(
        <MobileActionSheetProvider>
          <div data-message-id="m1">
            <span data-testid="start">alpha </span>
            <code>special</code>
            <span data-testid="end"> omega</span>
          </div>
          <SelectionPopup onAdd={() => {}} />
        </MobileActionSheetProvider>,
      ));

      stubSelectedMessageText("alpha special omega");

      await waitFor(() => {
        expect(captured.some((listener) => listener.type === "mouseup")).toBe(true);
      });
      await act(async () => {
        captured.find((listener) => listener.type === "mouseup")!.fn(
          new MouseEvent("mouseup", { bubbles: true }),
        );
      });

      await screen.findByText("Copy");
      expect(screen.queryByText("Cancel")).toBeNull();
    } finally {
      unmount();
      restore();
    }
  });
});
