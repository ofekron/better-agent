import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChangesPanel } from "../src/components/ChangesPanel";
import { eventBus } from "../src/lib/eventBus";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, value?: string | Record<string, unknown>) => {
      if (key === "rightPanel.changesSummaryTurns" && typeof value === "object") {
        return `${value.changes} changes · ${value.turns} turns`;
      }
      if (key === "rightPanel.changesTurn" && typeof value === "object") {
        return `Turn ${value.n}`;
      }
      if (typeof value === "string") return value;
      if (value && typeof value.defaultValue === "string") return value.defaultValue;
      return key;
    },
  }),
}));

const realFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = realFetch;
  vi.restoreAllMocks();
});

describe("ChangesPanel", () => {
  it("renders backend-grouped turns and refetches on provenance changes", async () => {
    const responses = [
      {
        session_id: "s1",
        turns: [
          {
            turn_index: 0,
            user_prompt: "first prompt",
            ts: "2026-06-28T12:00:00Z",
            changes: [
              {
                uuid: "c1",
                tool: "Edit",
                kind: "edit",
                file_path: "/tmp/a.ts",
                edits: [{ old_string: "old", new_string: "new" }],
                why: "because first",
                ts: "2026-06-28T12:00:01Z",
                msg_id: "assistant-1",
              },
            ],
          },
          {
            turn_index: 1,
            user_prompt: "second prompt",
            ts: "2026-06-28T12:10:00Z",
            changes: [
              {
                uuid: "c2",
                tool: "Edit",
                kind: "edit",
                file_path: "/tmp/b.ts",
                edits: [{ old_string: "before", new_string: "after" }],
                why: "because second",
                ts: "2026-06-28T12:10:01Z",
                msg_id: "assistant-2",
              },
            ],
          },
        ],
      },
      {
        session_id: "s1",
        turns: [
          {
            turn_index: 0,
            user_prompt: "first prompt",
            ts: "2026-06-28T12:00:00Z",
            changes: [],
          },
        ],
      },
    ];
    globalThis.fetch = vi.fn(async () => ({
      ok: true,
      json: async () => responses.shift(),
    })) as unknown as typeof fetch;

    render(<ChangesPanel sessionId="s1" />);

    await screen.findByText("2 changes · 2 turns");
    expect(screen.getByText("Turn 2")).toBeTruthy();
    expect(screen.getByText("second prompt")).toBeTruthy();

    fireEvent.click(screen.getByText("second prompt"));
    expect(screen.getByText("b.ts")).toBeTruthy();
    expect(screen.getByText("because second")).toBeTruthy();

    act(() => {
      eventBus.publish("session_provenance_changed", { session_id: "s1" });
    });
    await waitFor(() => expect(screen.getByText("No file edits yet.")).toBeTruthy());
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
  });
});
