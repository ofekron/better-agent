import { describe, expect, it } from "vitest";
import { renderHook } from "@testing-library/react";
import { useLatestEventOfTypes } from "../src/hooks/useLatestEventOfTypes";
import type { WSEvent } from "../src/types";

const TYPES = Object.freeze(["workers_changed"]);
const ROUTINE_TYPES = Object.freeze(["tasks_changed", "task_output_published"]);

function ev(type: string, data: Record<string, unknown> = {}): WSEvent {
  return { type: type as WSEvent["type"], data };
}

describe("useLatestEventOfTypes", () => {
  it("keeps referential identity stable while only non-matching tokens append", () => {
    let events: WSEvent[] = [ev("agent_message")];
    const { result, rerender } = renderHook(
      ({ e }) => useLatestEventOfTypes(e, TYPES),
      { initialProps: { e: events } },
    );
    const first = result.current;
    expect(first).toEqual([]);

    // Simulate token stream: buffer grows with irrelevant events every frame.
    for (let i = 0; i < 50; i++) {
      events = [...events, ev("agent_message", { i })];
      rerender({ e: events });
    }
    // Output identity never changed — dependent contexts stay stable.
    expect(result.current).toBe(first);
  });

  it("changes identity only when a matching event arrives, and exposes it as the tail", () => {
    let events: WSEvent[] = [ev("agent_message")];
    const { result, rerender } = renderHook(
      ({ e }) => useLatestEventOfTypes(e, TYPES),
      { initialProps: { e: events } },
    );
    const empty = result.current;

    events = [...events, ev("workers_changed", { n: 1 })];
    rerender({ e: events });
    const afterFirst = result.current;
    expect(afterFirst).not.toBe(empty);
    expect(afterFirst).toHaveLength(1);
    expect(afterFirst[0].data).toEqual({ n: 1 });

    // More non-matching tokens: identity holds.
    events = [...events, ev("agent_message"), ev("thinking")];
    rerender({ e: events });
    expect(result.current).toBe(afterFirst);

    // A second matching event flips identity and surfaces the newest one.
    events = [...events, ev("workers_changed", { n: 2 })];
    rerender({ e: events });
    expect(result.current).not.toBe(afterFirst);
    expect(result.current[0].data).toEqual({ n: 2 });
  });

  it("returns the newest matching event when several are appended in one frame", () => {
    const events: WSEvent[] = [
      ev("tasks_changed", { k: 1 }),
      ev("agent_message"),
      ev("task_output_published", { task_id: "t9" }),
    ];
    const { result } = renderHook(() => useLatestEventOfTypes(events, ROUTINE_TYPES));
    expect(result.current).toHaveLength(1);
    expect(result.current[0].type).toBe("task_output_published");
    expect(result.current[0].data).toEqual({ task_id: "t9" });
  });

  it("resets when the buffer shrinks (turn_start clears events to [])", () => {
    let events: WSEvent[] = [ev("workers_changed", { n: 1 })];
    const { result, rerender } = renderHook(
      ({ e }) => useLatestEventOfTypes(e, TYPES),
      { initialProps: { e: events } },
    );
    expect(result.current).toHaveLength(1);

    // turn_start wipes the buffer.
    events = [];
    rerender({ e: events });
    expect(result.current).toEqual([]);

    // A fresh non-matching token must NOT resurrect the old match.
    events = [ev("agent_message")];
    rerender({ e: events });
    expect(result.current).toEqual([]);
  });

  it("returns a stable empty array reference when nothing matches", () => {
    const a: WSEvent[] = [ev("agent_message")];
    const first = renderHook(() => useLatestEventOfTypes(a, TYPES));
    const second = renderHook(() => useLatestEventOfTypes([ev("thinking")], TYPES));
    expect(first.result.current).toBe(second.result.current);
  });
});
