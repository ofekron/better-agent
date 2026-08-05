import { describe, it, expect, beforeEach } from "vitest";
import { render, fireEvent } from "@testing-library/react";

import { WorkingModeLayout } from "../src/components/WorkingModeLayout";

/**
 * WorkingModeLayout unit slice — pure layout shell.
 *
 * useResizable runs REAL (localStorage-backed; setup.ts installs an
 * in-memory Storage). This asserts the layout/resizer wiring and the
 * hook integration (restore, default fallback, persistence, reverse
 * drag direction, min/max clamping) end-to-end, not just that lines ran.
 *
 * i18n runs REAL with empty resources — t("workingMode.dragToResize")
 * returns the key verbatim, which is assertable as-is.
 */

beforeEach(() => {
  localStorage.clear();
});

function fileviewerWidth(container: HTMLElement): number {
  const el = container.querySelector(".prompt-eng-fileviewer") as HTMLElement;
  return parseInt(el.style.width, 10);
}

describe("WorkingModeLayout", () => {
  it("renders badge, actions, and slots in the topbar/body (fileFirst default false)", () => {
    const { getByText, container } = render(
      <WorkingModeLayout
        storagePrefix="test"
        badge={<span>badge-text</span>}
        actions={<button>act</button>}
        chatSlot={<div>chat-content</div>}
        fileViewerSlot={<div>fv-content</div>}
      />,
    );
    expect(getByText("badge-text")).toBeTruthy();
    expect(getByText("act")).toBeTruthy();
    expect(getByText("chat-content")).toBeTruthy();
    expect(getByText("fv-content")).toBeTruthy();
    // chat-first ordering: chat precedes resizer precedes fileviewer
    const body = container.querySelector(".prompt-eng-body")!;
    const children = Array.from(body.children).map((c) => c.className);
    expect(children[0]).toBe("prompt-eng-chat");
    expect(children[1]).toBe("prompt-eng-resizer");
    expect(children[2]).toBe("prompt-eng-fileviewer");
  });

  it("renders fileviewer-first ordering when fileFirst is true", () => {
    const { container } = render(
      <WorkingModeLayout
        storagePrefix="test"
        fileFirst
        badge={<span>b</span>}
        actions={<span>a</span>}
        chatSlot={<span>c</span>}
        fileViewerSlot={<span>f</span>}
      />,
    );
    const body = container.querySelector(".prompt-eng-body")!;
    const children = Array.from(body.children).map((c) => c.className);
    expect(children[0]).toBe("prompt-eng-fileviewer prompt-eng-fileviewer-primary");
    expect(children[1]).toBe("prompt-eng-resizer");
    expect(children[2]).toBe("prompt-eng-chat prompt-eng-chat-secondary");
  });

  it("omits the bottom bar when bottomBar is not provided", () => {
    const { container } = render(
      <WorkingModeLayout
        storagePrefix="test"
        badge={<span>b</span>}
        actions={<span>a</span>}
        chatSlot={<span>c</span>}
        fileViewerSlot={<span>f</span>}
      />,
    );
    expect(container.querySelector(".prompt-eng-bottombar")).toBeNull();
  });

  it("renders the bottom bar when bottomBar is provided", () => {
    const { getByText, container } = render(
      <WorkingModeLayout
        storagePrefix="test"
        badge={<span>b</span>}
        actions={<span>a</span>}
        chatSlot={<span>c</span>}
        fileViewerSlot={<span>f</span>}
        bottomBar={<div>bottom-content</div>}
      />,
    );
    expect(getByText("bottom-content")).toBeTruthy();
    expect(container.querySelector(".prompt-eng-bottombar")).not.toBeNull();
  });

  it("applies default className and testId, and custom values when passed", () => {
    const defaultRoot = render(
      <WorkingModeLayout
        storagePrefix="test"
        badge={<span>b</span>}
        actions={<span>a</span>}
        chatSlot={<span>c</span>}
        fileViewerSlot={<span>f</span>}
      />,
    );
    expect(defaultRoot.container.firstElementChild!.className).toBe("working-mode-overlay");
    expect(defaultRoot.container.firstElementChild!.getAttribute("data-testid")).toBeNull();
    defaultRoot.unmount();

    const customRoot = render(
      <WorkingModeLayout
        storagePrefix="test"
        className="custom-overlay"
        testId="wm-root"
        badge={<span>b</span>}
        actions={<span>a</span>}
        chatSlot={<span>c</span>}
        fileViewerSlot={<span>f</span>}
      />,
    );
    expect(customRoot.container.firstElementChild!.className).toBe("custom-overlay");
    expect(customRoot.container.firstElementChild!.getAttribute("data-testid")).toBe("wm-root");
  });

  it("falls back to defaultSize=520 on first open (no localStorage value)", () => {
    const { container } = render(
      <WorkingModeLayout
        storagePrefix="test"
        badge={<span>b</span>}
        actions={<span>a</span>}
        chatSlot={<span>c</span>}
        fileViewerSlot={<span>f</span>}
      />,
    );
    expect(fileviewerWidth(container)).toBe(520);
  });

  it("honors a custom defaultSize prop", () => {
    const { container } = render(
      <WorkingModeLayout
        storagePrefix="test"
        defaultSize={400}
        badge={<span>b</span>}
        actions={<span>a</span>}
        chatSlot={<span>c</span>}
        fileViewerSlot={<span>f</span>}
      />,
    );
    expect(fileviewerWidth(container)).toBe(400);
  });

  it("restores the persisted width from localStorage on mount", () => {
    localStorage.setItem("test.fileViewerWidth", "700");
    const { container } = render(
      <WorkingModeLayout
        storagePrefix="test"
        badge={<span>b</span>}
        actions={<span>a</span>}
        chatSlot={<span>c</span>}
        fileViewerSlot={<span>f</span>}
      />,
    );
    expect(fileviewerWidth(container)).toBe(700);
  });

  it("clamps an out-of-range persisted value to [min,max] on read", () => {
    localStorage.setItem("over.fileViewerWidth", "99999");
    const { container: over } = render(
      <WorkingModeLayout
        storagePrefix="over"
        badge={<span>b</span>}
        actions={<span>a</span>}
        chatSlot={<span>c</span>}
        fileViewerSlot={<span>f</span>}
      />,
    );
    expect(fileviewerWidth(over)).toBe(1200);

    localStorage.setItem("under.fileViewerWidth", "10");
    const { container: under } = render(
      <WorkingModeLayout
        storagePrefix="under"
        badge={<span>b</span>}
        actions={<span>a</span>}
        chatSlot={<span>c</span>}
        fileViewerSlot={<span>f</span>}
      />,
    );
    expect(fileviewerWidth(under)).toBe(280);
  });

  it("persists the resolved width to localStorage after mount", () => {
    render(
      <WorkingModeLayout
        storagePrefix="persist"
        defaultSize={333}
        badge={<span>b</span>}
        actions={<span>a</span>}
        chatSlot={<span>c</span>}
        fileViewerSlot={<span>f</span>}
      />,
    );
    expect(localStorage.getItem("persist.fileViewerWidth")).toBe("333");
  });

  it("wires the resizer: title, testid, and reverse-direction drag that resizes + clamps + persists", () => {
    const { container } = render(
      <WorkingModeLayout
        storagePrefix="drag"
        badge={<span>b</span>}
        actions={<span>a</span>}
        chatSlot={<span>c</span>}
        fileViewerSlot={<span>f</span>}
      />,
    );
    const resizer = container.querySelector(".prompt-eng-resizer") as HTMLElement;
    expect(resizer.getAttribute("data-testid")).toBe("drag-resizer");
    expect(resizer.getAttribute("title")).toBe("workingMode.dragToResize");

    // reverse direction: dragging the cursor LEFT increases width.
    // start size 520 at clientX 600; move to clientX 400 → delta +200 → 720.
    fireEvent.mouseDown(resizer, { clientX: 600 });
    expect(document.body.style.cursor).toBe("col-resize");
    fireEvent.mouseMove(document, { clientX: 400 });
    expect(fileviewerWidth(container)).toBe(720);

    // continue dragging far left → clamps to max 1200 and persists.
    fireEvent.mouseMove(document, { clientX: -100000 });
    expect(fileviewerWidth(container)).toBe(1200);
    fireEvent.mouseUp(document);
    expect(document.body.style.cursor).toBe("");
    expect(localStorage.getItem("drag.fileViewerWidth")).toBe("1200");
  });

  it("clamps to min when a reverse drag pushes width below the floor", () => {
    const { container } = render(
      <WorkingModeLayout
        storagePrefix="min"
        badge={<span>b</span>}
        actions={<span>a</span>}
        chatSlot={<span>c</span>}
        fileViewerSlot={<span>f</span>}
      />,
    );
    const resizer = container.querySelector(".prompt-eng-resizer") as HTMLElement;
    // reverse: dragging RIGHT shrinks. start 520 @ 0 → move to 100000 → -100000 delta → clamp 280.
    fireEvent.mouseDown(resizer, { clientX: 0 });
    fireEvent.mouseMove(document, { clientX: 100000 });
    expect(fileviewerWidth(container)).toBe(280);
    fireEvent.mouseUp(document);
  });
});
