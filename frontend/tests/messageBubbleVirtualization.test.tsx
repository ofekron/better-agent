import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { MessageBubble } from "../src/components/MessageBubble";
import { VIRTUALIZE_EVENT_THRESHOLD } from "../src/components/VirtualizedEventList";
import { makeAssistantMsg } from "./fixtures";
import type { WSEvent } from "../src/types";

// Integration-level: the v2-path threshold gate (below VIRTUALIZE_EVENT_
// THRESHOLD rows -> plain, unchanged DOM regardless of flag; above it ->
// VirtualizedEventList mounts, ONLY when the surface_v2 flag is on — the
// legacy path stays fully mounted no matter the row count). The specific
// "renders window at the right indices" behavior is unit-tested in
// tests/virtualizedEventList.test.tsx against a controlled virtualizer;
// this file never asserts on real jsdom/happy-dom layout geometry.

function outputEvents(n: number): WSEvent[] {
  return Array.from({ length: n }, (_, i) => ({
    type: "output",
    data: { output: `plain output line number ${i}`, node_id: `n${i}` },
  })) as WSEvent[];
}

function renderBubble(events: WSEvent[]) {
  return render(
    <MessageBubble
      message={makeAssistantMsg({
        id: "fixed-msg-id",
        timestamp: "2026-01-01T00:00:00.000Z",
        events,
        isStreaming: false,
      })}
    />,
  );
}

beforeEach(() => {
  localStorage.removeItem("ba.surface_v2");
});

afterEach(() => {
  cleanup();
  localStorage.setItem("ba.surface_v2", "0");
});

describe("virtualization threshold gating (v2 path only)", () => {
  it("renders byte-identical DOM below the threshold whether the flag is on or off", () => {
    const events = outputEvents(50);
    expect(events.length).toBeLessThan(VIRTUALIZE_EVENT_THRESHOLD);

    localStorage.setItem("ba.surface_v2", "1");
    const on = renderBubble(events);
    const onHtml = on.container.innerHTML;
    expect(on.container.querySelectorAll(".message-box")).toHaveLength(50);
    cleanup();

    localStorage.setItem("ba.surface_v2", "0");
    const off = renderBubble(events);
    const offHtml = off.container.innerHTML;
    expect(off.container.querySelectorAll(".message-box")).toHaveLength(50);

    expect(onHtml).toBe(offHtml);
    expect(on.container.querySelector('[data-testid^="virtualized-event-list"]')).toBeNull();
  });

  it("switches to VirtualizedEventList above the threshold, but only on the v2 (flag-on) path", () => {
    const events = outputEvents(VIRTUALIZE_EVENT_THRESHOLD + 50);

    localStorage.setItem("ba.surface_v2", "1");
    const on = renderBubble(events);
    expect(on.container.querySelector('[data-testid^="virtualized-event-list"]')).not.toBeNull();
    cleanup();

    localStorage.setItem("ba.surface_v2", "0");
    const off = renderBubble(events);
    expect(off.container.querySelector('[data-testid^="virtualized-event-list"]')).toBeNull();
    // Legacy path: every row is still fully, individually mounted —
    // "legacy rendering untouched" holds even for a huge event count.
    expect(off.container.querySelectorAll(".message-box")).toHaveLength(VIRTUALIZE_EVENT_THRESHOLD + 50);
  });

  it("preserves every row's content above the threshold on the v2 path (no silent data loss)", () => {
    const n = VIRTUALIZE_EVENT_THRESHOLD + 20;
    const events = outputEvents(n);
    localStorage.setItem("ba.surface_v2", "1");
    const { container } = renderBubble(events);

    expect(container.querySelector('[data-testid^="virtualized-event-list"]')).not.toBeNull();
    // No real scrollable ancestor exists in this render tree, so
    // VirtualizedEventList's own "no scroll owner yet" fallback mounts
    // every row (see its module docstring) — this asserts nothing was
    // dropped by the switch to the virtualized wrapper itself.
    const text = container.textContent ?? "";
    for (let i = 0; i < n; i++) {
      expect(text).toContain(`plain output line number ${i}`);
    }
  });
});
