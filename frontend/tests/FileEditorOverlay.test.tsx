import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import i18n from "i18next";

import type { FileEditingState } from "../src/types/fileEditing";
import { SIDEBAR_MINIMIZED_WIDTH } from "../src/sidebarLayout";

/**
 * FileEditorOverlay unit slice.
 *
 * Isolation: WorkingModeLayout is a sibling component (own coverage file,
 * pulls in the localStorage-backed useResizable hook) — mocked to a thin
 * passthrough that surfaces badge/actions/slots/defaultSize so this test
 * exercises FileEditorOverlay's OWN logic, not the layout's resizing.
 * useBackButtonDismiss is a history-manipulating hook — mocked to capture
 * the handleBack callback it's handed so we can drive handleBack directly.
 *
 * i18n runs REAL: the fileEditor.* EN bundle is added in-test so
 * {{fileName}}/{{count}} interpolation is asserted on formatted data.
 */

const { backRef } = vi.hoisted(() => ({ backRef: { current: null as null | (() => void) } }));

vi.mock("../src/components/WorkingModeLayout", () => ({
  WorkingModeLayout: (props: {
    testId?: string;
    defaultSize?: number;
    fileFirst?: boolean;
    badge: unknown;
    actions: unknown;
    chatSlot: unknown;
    fileViewerSlot: unknown;
  }) => (
    <div
      data-testid={props.testId}
      data-default-size={String(props.defaultSize)}
      data-file-first={String(props.fileFirst ?? false)}
    >
      <div data-mock="badge">{props.badge}</div>
      <div data-mock="actions">{props.actions}</div>
      <div data-mock="chat">{props.chatSlot}</div>
      <div data-mock="fileviewer">{props.fileViewerSlot}</div>
    </div>
  ),
}));

vi.mock("../src/hooks/useBackButtonDismiss", () => ({
  useBackButtonDismiss: (_open: boolean, onClose: () => void) => {
    backRef.current = onClose;
  },
}));

import { FileEditorOverlay } from "../src/components/FileEditorOverlay";

const FILE_EDITOR_EN: Record<string, string> = {
  "fileEditor.badge": "✏️ Editing — {{fileName}}",
  "fileEditor.badgeMulti": "✏️ Editing — {{count}} files",
  "fileEditor.discarding": "Discarding…",
  "fileEditor.discard": "Discard",
  "fileEditor.closing": "Closing…",
  "fileEditor.done": "Done",
};

function loadI18n(): void {
  i18n.addResourceBundle("en", "translation", FILE_EDITOR_EN, true, true);
}

function deferred<T = void>(): {
  promise: Promise<T>;
  resolve: (v: T) => void;
  reject: (e: unknown) => void;
} {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function makeState(files: string[]): FileEditingState {
  return {
    sessionId: "s1",
    filePaths: files,
    originalContents: {},
    fileDiscussions: [],
  };
}

function overlay(props: {
  files?: string[];
  persistent?: boolean;
  onDone?: () => Promise<void>;
  onCancel?: () => Promise<void>;
  chatSlot?: React.ReactNode;
  fileViewerSlot?: React.ReactNode;
}) {
  return render(
    <FileEditorOverlay
      state={makeState(props.files ?? ["src/app.tsx"])}
      persistent={props.persistent ?? false}
      onDone={props.onDone ?? (async () => {})}
      onCancel={props.onCancel ?? (async () => {})}
      chatSlot={props.chatSlot ?? <span data-mock="chat-content" />}
      fileViewerSlot={props.fileViewerSlot ?? <span data-mock="fv-content" />}
    />,
  );
}

const origInnerWidth = window.innerWidth;

beforeEach(() => {
  loadI18n();
  backRef.current = null;
  window.innerWidth = 1024;
});

afterEach(() => {
  window.innerWidth = origInnerWidth;
});

describe("FileEditorOverlay — initial file-viewer width", () => {
  it("clamps to the 500px floor when the viewport is narrow", () => {
    window.innerWidth = 600; // floor((600-52)/2)=274 -> clamp 500
    const { container } = overlay({});
    const root = container.querySelector('[data-testid="file-editor-overlay"]')!;
    expect(root.getAttribute("data-default-size")).toBe("500");
  });

  it("uses the 50/50 split when the viewport is wide", () => {
    window.innerWidth = 2000; // floor((2000-52)/2)=974
    const { container } = overlay({});
    const root = container.querySelector('[data-testid="file-editor-overlay"]')!;
    expect(root.getAttribute("data-default-size")).toBe(
      String(Math.floor((2000 - SIDEBAR_MINIMIZED_WIDTH) / 2)),
    );
  });
});

describe("FileEditorOverlay — badge", () => {
  it("shows the single-file badge with the derived file name", () => {
    const { container } = overlay({ files: ["src/components/Foo.tsx"] });
    expect(container.querySelector('[data-mock="badge"]')!.textContent).toBe(
      "✏️ Editing — Foo.tsx",
    );
  });

  it("strips a trailing slash before deriving the name", () => {
    const { container } = overlay({ files: ["src/components/"] });
    expect(container.querySelector('[data-mock="badge"]')!.textContent).toBe(
      "✏️ Editing — components",
    );
  });

  it("falls back to the raw path when the name is empty", () => {
    // filePaths[0] ?? "" -> "" (empty-path branch); pop() "" falsy -> `|| first`.
    const { container } = overlay({ files: [""] });
    expect(container.querySelector('[data-mock="badge"]')!.textContent).toBe(
      "✏️ Editing — ",
    );
  });

  it("uses the ?? '' fallback when filePaths is empty", () => {
    // filePaths[0] is undefined -> nullish-coalesce right branch.
    const { container } = overlay({ files: [] });
    expect(container.querySelector('[data-mock="badge"]')!.textContent).toBe(
      "✏️ Editing — ",
    );
  });

  it("shows the multi-file badge with the count when several files", () => {
    const { container } = overlay({ files: ["a.txt", "b.txt", "c.txt"] });
    expect(container.querySelector('[data-mock="badge"]')!.textContent).toBe(
      "✏️ Editing — 3 files",
    );
  });
});

describe("FileEditorOverlay — layout wiring", () => {
  it("renders with fileFirst and the overlay test id, forwarding both slots", () => {
    const { container } = overlay({
      chatSlot: <span data-mock="chat-x" />,
      fileViewerSlot: <span data-mock="fv-x" />,
    });
    const root = container.querySelector('[data-testid="file-editor-overlay"]')!;
    expect(root.getAttribute("data-file-first")).toBe("true");
    expect(container.querySelector('[data-mock="chat-x"]')).not.toBeNull();
    expect(container.querySelector('[data-mock="fv-x"]')).not.toBeNull();
  });
});

describe("FileEditorOverlay — Done button (persistent flavor)", () => {
  it("hides the Done button when persistent", () => {
    const { container } = overlay({ persistent: true });
    expect(container.querySelector('[data-testid="file-editor-done-btn"]')).toBeNull();
    expect(
      container.querySelector('[data-testid="file-editor-cancel-btn"]'),
    ).not.toBeNull();
  });

  it("calls onDone and toggles busy label on click", async () => {
    const onDone = vi.fn(async () => {});
    const { container } = overlay({ onDone });
    const btn = container.querySelector(
      '[data-testid="file-editor-done-btn"]',
    ) as HTMLButtonElement;
    expect(btn.textContent).toBe("Done");
    fireEvent.click(btn);
    await waitFor(() => expect(btn.textContent).toBe("Closing…"));
    expect(onDone).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(btn.textContent).toBe("Done"));
    expect(btn.disabled).toBe(false);
  });

  it("holds the UI lock (disabled) for the whole onDone window", async () => {
    const gate = deferred<void>();
    const onDone = vi.fn(() => gate.promise);
    const { container } = overlay({ onDone });
    const btn = container.querySelector(
      '[data-testid="file-editor-done-btn"]',
    ) as HTMLButtonElement;
    fireEvent.click(btn);
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
    // The button stays disabled (busy !== null) until onDone resolves —
    // that disabled lock is the real re-entrancy protection.
    await waitFor(() => expect(btn.disabled).toBe(true));
    expect(btn.textContent).toBe("Closing…");
    fireEvent.click(btn); // no-op: button is disabled mid-flight
    expect(onDone).toHaveBeenCalledTimes(1);
    gate.resolve(undefined);
    await waitFor(() => expect(btn.disabled).toBe(false));
    expect(btn.textContent).toBe("Done");
  });
});

describe("FileEditorOverlay — Discard (cancel) button", () => {
  it("calls onCancel and toggles busy labels on click", async () => {
    const onCancel = vi.fn(async () => {});
    const { container } = overlay({ onCancel });
    const btn = container.querySelector(
      '[data-testid="file-editor-cancel-btn"]',
    ) as HTMLButtonElement;
    expect(btn.textContent).toBe("Discard");
    fireEvent.click(btn);
    await waitFor(() => expect(btn.textContent).toBe("Discarding…"));
    expect(onCancel).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(btn.textContent).toBe("Discard"));
    expect(btn.disabled).toBe(false);
  });

  it("guards against a second concurrent onCancel while busy", async () => {
    const gate = deferred<void>();
    const onCancel = vi.fn(() => gate.promise);
    const { container } = overlay({ onCancel });
    const btn = container.querySelector(
      '[data-testid="file-editor-cancel-btn"]',
    ) as HTMLButtonElement;
    fireEvent.click(btn);
    await waitFor(() => expect(onCancel).toHaveBeenCalledTimes(1));
    // The button stays disabled (busy !== null) until onCancel resolves.
    await waitFor(() => expect(btn.disabled).toBe(true));
    expect(btn.textContent).toBe("Discarding…");
    fireEvent.click(btn); // no-op: button is disabled mid-flight
    expect(onCancel).toHaveBeenCalledTimes(1);
    gate.resolve(undefined);
    await waitFor(() => expect(btn.disabled).toBe(false));
  });
});

describe("FileEditorOverlay — hardware back button (handleBack)", () => {
  it("awaits onCancel and clears the in-flight sentinel it pushed", async () => {
    const onCancel = vi.fn(async () => {});
    overlay({ onCancel });
    expect(backRef.current).not.toBeNull();

    await backRef.current!();
    expect(onCancel).toHaveBeenCalledTimes(1);
    // sentinel pushed then cleaned up -> state has no __cancelInFlight
    expect(
      (window.history.state as { __cancelInFlight?: boolean } | null)
        ?.__cancelInFlight ?? false,
    ).toBe(false);
  });

  it("leaves history state untouched if another writer replaced the sentinel mid-flight", async () => {
    // onCancel clobbers history state during the await window, so handleBack's
    // post-await check finds no __cancelInFlight and skips its replaceState.
    const onCancel = vi.fn(async () => {
      window.history.replaceState({ otherWriter: true }, "");
    });
    overlay({ onCancel });
    await backRef.current!();
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(window.history.state).toEqual({ otherWriter: true });
  });
});
