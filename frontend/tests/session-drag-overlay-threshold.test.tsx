import { describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { SessionList } from "../src/components/SessionList";
import { eventBus } from "../src/lib/eventBus";
import type { Session } from "../src/types";
import { shouldStartAgentBoardSessionDrag } from "../src/utils/sessionDragThreshold";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

function session(extra: Partial<Session> = {}): Session {
  return {
    id: "session-1",
    name: "Dragged Session",
    model: "model",
    cwd: "",
    created_at: "2026-01-01T00:00:00.000Z",
    updated_at: "2026-01-01T00:00:00.000Z",
    messages: [],
    ...extra,
  };
}

function renderList() {
  return render(
    <SessionList
      sessions={[session()]}
      providers={[]}
      onSelect={() => {}}
      onDelete={() => {}}
      onRename={() => {}}
      onPin={() => {}}
      onArchive={() => {}}
      onWorkerEligible={() => {}}
      onAgentRenameAllowed={() => {}}
      onDetails={() => {}}
      onUnpinOthers={() => {}}
    />,
  );
}

function dataTransfer() {
  const values = new Map<string, string>();
  return {
    types: [] as string[],
    effectAllowed: "move",
    dropEffect: "move",
    setData(type: string, value: string) {
      values.set(type, value);
      if (!this.types.includes(type)) this.types.push(type);
    },
    getData(type: string) {
      return values.get(type) ?? "";
    },
  };
}

function dragEvent(type: string, clientX: number, clientY: number, transfer: ReturnType<typeof dataTransfer>) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperties(event, {
    clientX: { value: clientX },
    clientY: { value: clientY },
    dataTransfer: { value: transfer },
  });
  return event;
}

describe("agent-board session drag threshold", () => {
  it("does not start the board overlay for a small native drag", () => {
    expect(
      shouldStartAgentBoardSessionDrag(
        { clientX: 10, clientY: 10 },
        { clientX: 40, clientY: 10 },
      ),
    ).toBe(false);
  });

  it("starts the board overlay after a deliberate drag", () => {
    expect(
      shouldStartAgentBoardSessionDrag(
        { clientX: 10, clientY: 10 },
        { clientX: 59, clientY: 10 },
      ),
    ).toBe(true);
  });

  it("does not start without a captured pointer origin", () => {
    expect(
      shouldStartAgentBoardSessionDrag(null, { clientX: 100, clientY: 100 }),
    ).toBe(false);
  });

  it("publishes the board overlay drag start only after threshold movement", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ folders: [], tags: [], models: [] }))));
    const starts: Array<{ session_id: string; name?: string }> = [];
    const off = eventBus.subscribe("session_drag_start", (payload) => starts.push(payload));
    try {
      renderList();
      const row = screen.getByText("Dragged Session").closest(".session-item");
      expect(row).not.toBeNull();
      await waitFor(() => expect(row?.getAttribute("draggable")).toBe("true"));
      const transfer = dataTransfer();

      act(() => {
        row!.dispatchEvent(dragEvent("dragstart", 10, 10, transfer));
        document.dispatchEvent(dragEvent("dragover", 40, 10, transfer));
      });
      expect(starts).toEqual([]);

      act(() => {
        document.dispatchEvent(dragEvent("dragover", 59, 10, transfer));
      });
      expect(starts).toEqual([{ session_id: "session-1", name: "Dragged Session" }]);

      act(() => {
        document.dispatchEvent(dragEvent("dragover", 80, 10, transfer));
      });
      expect(starts).toHaveLength(1);
    } finally {
      off();
      vi.unstubAllGlobals();
    }
  });
});
