import { describe, it, expect } from "vitest";
import { tagEvents } from "../src/utils/mergeEvents";
import type { WSEvent, WorkerPanel } from "../src/types";
import fixture from "./__fixtures__/real_panel_ordering.json";

// Real render-tree data captured from session 1bf5ac54 (the create_sub_session
// -> ask combo the user reported). Proves the SHIPPING tagEvents places each
// delegation panel AFTER its triggering tool_use on real data.

type Turn = { events: WSEvent[]; workers: WorkerPanel[] };

const toolNameAt = (e: WSEvent): string | null => {
  const c = (e.data as any)?.message?.content;
  if (!Array.isArray(c)) return null;
  const tu = c.find((b: any) => b?.type === "tool_use");
  return tu ? tu.name : null;
};

/** Linearize the tagged stream: manager entries -> their tool name (or "·"),
 * worker entries -> "W:<delegation_id>". */
const linear = (turn: Turn): string[] =>
  tagEvents(turn.events, turn.workers).map((t) =>
    t.entityType === "worker" ? `W:${t.entityId}` : toolNameAt(t.event) ?? "·",
  );

const lastIndex = (arr: string[], pred: (s: string) => boolean) => {
  let r = -1;
  arr.forEach((s, i) => pred(s) && (r = i));
  return r;
};

describe("real session create_sub_session -> ask panel ordering", () => {
  it("seq3: the sub_session_created marker renders AFTER create_sub_session", () => {
    const seq = linear((fixture as any).seq3);
    const createIdx = seq.findIndex((s) => /create_sub_session/.test(s));
    const markerIdx = seq.findIndex((s) => s.startsWith("W:created_"));
    expect(createIdx).toBeGreaterThanOrEqual(0);
    expect(markerIdx).toBeGreaterThanOrEqual(0);
    // The bug: marker rendered BEFORE create_sub_session. Must now be after.
    expect(markerIdx).toBeGreaterThan(createIdx);
  });

  it("seq5: every ask sub-session panel renders AFTER an ask tool call", () => {
    const seq = linear((fixture as any).seq5);
    const firstAsk = seq.findIndex((s) => /__ask$/.test(s));
    expect(firstAsk).toBeGreaterThanOrEqual(0);
    seq.forEach((s, i) => {
      if (s.startsWith("W:team_ask")) {
        // a panel must not appear before the first ask call
        expect(i).toBeGreaterThan(firstAsk);
      }
    });
    // and the last panel sits after the last ask call
    const lastPanel = lastIndex(seq, (s) => s.startsWith("W:team_ask"));
    const lastAsk = lastIndex(seq, (s) => /__ask$/.test(s));
    expect(lastPanel).toBeGreaterThan(lastAsk);
  });
});
