import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionList } from "../src/components/SessionList";
import type { Provider, Session } from "../src/types";
import { makeSession } from "./fixtures";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

const providers: Provider[] = [];

function renderList(
  sessions: Session[],
  props: Partial<ComponentProps<typeof SessionList>> = {},
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => new Promise<Response>(() => {})),
  );
  return render(
    <SessionList
      sessions={sessions}
      providers={providers}
      onSelect={() => {}}
      onDelete={() => {}}
      onRename={() => {}}
      onPin={() => {}}
      onUnpinOthers={() => {}}
      onArchive={() => {}}
      onWorkerEligible={() => {}}
      onAgentRenameAllowed={() => {}}
      onDetails={() => {}}
      {...props}
    />,
  );
}

function rowBySessionId(id: string): HTMLElement {
  const row = screen.getAllByTestId("session-item").find(
    (item) => item.getAttribute("data-session-id") === id,
  );
  if (!row) throw new Error(`Session row not found: ${id}`);
  return row;
}

function makeDataTransfer(dropEffect: "none" | "move") {
  return {
    setData: vi.fn(),
    getData: () => "",
    effectAllowed: "uninitialized",
    dropEffect,
  };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// The browser never dispatches a click once a native HTML5 drag starts,
// and Chromium starts one after only a few px of pointer movement —
// far below the 48px agent-board drag threshold. These tests lock the
// dragend fallback that recovers such micro-drags as the click they
// really were. jsdom does not itself suppress clicks, so the tests
// simulate the browser sequence (pointerdown → dragstart → dragend,
// no click) directly.
describe("session row drag-swallowed click fallback", () => {
  it("selects the session when a drag ends below the threshold with no drop target", () => {
    const onSelect = vi.fn();
    const s = makeSession({ id: "s1", name: "Alpha" });
    renderList([s], { onSelect });
    const row = rowBySessionId("s1");
    const dataTransfer = makeDataTransfer("none");
    fireEvent.pointerDown(row, { button: 0, clientX: 10, clientY: 10 });
    fireEvent.dragStart(row, { clientX: 10, clientY: 10, dataTransfer });
    fireEvent.dragEnd(row, { clientX: 14, clientY: 12, dataTransfer });
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("s1");
  });

  it("does not select when the drag crossed the agent-board threshold", () => {
    const onSelect = vi.fn();
    const s = makeSession({ id: "s1", name: "Alpha" });
    renderList([s], { onSelect });
    const row = rowBySessionId("s1");
    const dataTransfer = makeDataTransfer("none");
    fireEvent.pointerDown(row, { button: 0, clientX: 10, clientY: 10 });
    fireEvent.dragStart(row, { clientX: 10, clientY: 10, dataTransfer });
    // Document-level dragover tracking records the drag's travel. jsdom
    // drops clientX/Y on synthesized drag events, so build one by hand
    // the way a real browser delivers it.
    const dragOver = new Event("dragover", { bubbles: true });
    Object.assign(dragOver, { clientX: 120, clientY: 10, dataTransfer });
    document.dispatchEvent(dragOver);
    fireEvent.dragEnd(row, { clientX: 120, clientY: 10, dataTransfer });
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("does not select when a drop target consumed the drag", () => {
    const onSelect = vi.fn();
    const s = makeSession({ id: "s1", name: "Alpha" });
    renderList([s], { onSelect });
    const row = rowBySessionId("s1");
    const dataTransfer = makeDataTransfer("move");
    fireEvent.pointerDown(row, { button: 0, clientX: 10, clientY: 10 });
    fireEvent.dragStart(row, { clientX: 10, clientY: 10, dataTransfer });
    fireEvent.dragEnd(row, { clientX: 14, clientY: 12, dataTransfer });
    expect(onSelect).not.toHaveBeenCalled();
  });
});
