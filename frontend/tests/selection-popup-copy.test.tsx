import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import React from "react";
import "../src/i18n";

vi.unmock("../src/components/SelectionPopup");
const { SelectionPopup } = await import("../src/components/SelectionPopup");
const { MobileActionSheetProvider } = await import("../src/components/MobileActionSheet");

afterEach(() => {
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

  it("leaves native Ctrl/Cmd+C authoritative while the range is active", async () => {
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

      const selection = stubSelectedMessageText("alpha special omega");

      await waitFor(() => {
        expect(captured.some((listener) => listener.type === "mouseup")).toBe(true);
      });
      await act(async () => {
        captured.find((listener) => listener.type === "mouseup")!.fn(
          new MouseEvent("mouseup", { bubbles: true }),
        );
      });
      await screen.findByText("Copy");
      expect(screen.getByRole("button", { name: "Copy" })).toBeTruthy();
      expect(screen.getByRole("button", { name: "Comment" })).toBeTruthy();
      expect(
        screen.queryByRole("button", { name: /adversarial sync/i }),
      ).toBeNull();

      const nativeCopyEvent = new KeyboardEvent("keydown", {
        key: "c",
        ctrlKey: true,
        bubbles: true,
        cancelable: true,
      });
      expect(document.dispatchEvent(nativeCopyEvent)).toBe(true);
      expect(nativeCopyEvent.defaultPrevented).toBe(false);
      expect(selection.removeAllRanges).not.toHaveBeenCalled();
      expect(writeText).not.toHaveBeenCalled();
    } finally {
      unmount();
      restore();
    }
  });

  it("keeps the selection and popup copy available after right-click", async () => {
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

      const selection = stubSelectedMessageText("  alpha special omega\n");

      await waitFor(() => {
        expect(captured.some((listener) => listener.type === "mouseup")).toBe(true);
        expect(captured.some((listener) => listener.type === "contextmenu")).toBe(true);
      });
      await act(async () => {
        captured.find((listener) => listener.type === "mouseup")!.fn(
          new MouseEvent("mouseup", { bubbles: true }),
        );
      });
      await screen.findByText("Copy");

      await act(async () => {
        captured.find((listener) => listener.type === "contextmenu")!.fn(
          new MouseEvent("contextmenu", { bubbles: true, cancelable: true }),
        );
      });

      expect(selection.removeAllRanges).not.toHaveBeenCalled();
      await act(async () => {
        screen.getByRole("button", { name: "Copy" }).click();
      });

      await waitFor(() => {
        expect(writeText).toHaveBeenCalledWith("  alpha special omega\n");
      });
    } finally {
      unmount();
      restore();
    }
  });

  it("does not clear a newer selection when popup copy resolves", async () => {
    const { captured, restore } = captureDocumentListeners();
    let resolveCopy = () => {};
    const writeText = vi.fn().mockImplementation(
      () => new Promise<void>((resolve) => {
        resolveCopy = resolve;
      }),
    );
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    let unmount = () => {};
    try {
      ({ unmount } = render(
        <>
          <div data-message-id="m1">
            <span data-testid="start">alpha special omega</span>
          </div>
          <SelectionPopup onAdd={() => {}} />
        </>,
      ));
      const originalSelection = stubSelectedMessageText("alpha special omega");
      await waitFor(() => {
        expect(captured.some((listener) => listener.type === "mouseup")).toBe(true);
      });
      await act(async () => {
        captured.find((listener) => listener.type === "mouseup")!.fn(
          new MouseEvent("mouseup", { bubbles: true }),
        );
      });
      await screen.findByRole("button", { name: "Copy" });
      screen.getByRole("button", { name: "Copy" }).click();

      const newerSelection = {
        isCollapsed: false,
        toString: () => "newer selection",
        removeAllRanges: vi.fn(),
      } as unknown as Selection;
      vi.mocked(window.getSelection).mockReturnValue(newerSelection);
      await act(async () => resolveCopy());

      expect(originalSelection.removeAllRanges).not.toHaveBeenCalled();
      expect(newerSelection.removeAllRanges).not.toHaveBeenCalled();
      expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
    } finally {
      unmount();
      restore();
    }
  });

  it("keeps native Ctrl+C available after right-click", async () => {
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

      const selection = stubSelectedMessageText("alpha special omega");

      await waitFor(() => {
        expect(captured.some((listener) => listener.type === "mouseup")).toBe(true);
        expect(captured.some((listener) => listener.type === "contextmenu")).toBe(true);
      });
      await act(async () => {
        captured.find((listener) => listener.type === "mouseup")!.fn(
          new MouseEvent("mouseup", { bubbles: true }),
        );
      });
      await screen.findByText("Copy");
      await act(async () => {
        captured.find((listener) => listener.type === "contextmenu")!.fn(
          new MouseEvent("contextmenu", { bubbles: true, cancelable: true }),
        );
      });
      const nativeCopyEvent = new KeyboardEvent("keydown", {
        key: "c",
        ctrlKey: true,
        bubbles: true,
        cancelable: true,
      });
      expect(document.dispatchEvent(nativeCopyEvent)).toBe(true);
      expect(nativeCopyEvent.defaultPrevented).toBe(false);
      expect(selection.removeAllRanges).not.toHaveBeenCalled();
      expect(writeText).not.toHaveBeenCalled();
    } finally {
      unmount();
      restore();
    }
  });

  it("does not copy stale text after Comment closes the popup", async () => {
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
      });
      await act(async () => {
        captured.find((listener) => listener.type === "mouseup")!.fn(
          new MouseEvent("mouseup", { bubbles: true }),
        );
      });
      await screen.findByText("Comment");
      await act(async () => {
        screen.getByRole("button", { name: "Comment" }).click();
      });
      expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
      expect(writeText).not.toHaveBeenCalled();
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
      act(() => {
        captured.find((listener) => listener.type === "touchend")!.fn(
          new TouchEvent("touchend", { bubbles: true }),
        );
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 450));
      });

      await screen.findByText("Cancel");
      expect(screen.getByRole("button", { name: "Copy" })).toBeTruthy();
      expect(screen.getByRole("button", { name: "Comment" })).toBeTruthy();
      expect(
        screen.queryByRole("button", { name: /adversarial sync/i }),
      ).toBeNull();
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
