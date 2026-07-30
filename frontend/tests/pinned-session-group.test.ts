import { describe, expect, it } from "vitest";
import { partitionSessions } from "../src/lib/sessionSort";
import type { Session } from "../src/types";

function session(id: string, extra: Partial<Session> = {}): Session {
  return { id, name: id, ...extra } as Session;
}

const byPinned = (s: Session) => Boolean(s.pinned);

describe("pinned session grouping", () => {
  it("hoists pinned sessions out of their folders into one group", () => {
    const { matched: pinned, rest: unpinned } = partitionSessions(
      [
        session("a", { folder_id: "f1", pinned: true }),
        session("b", { folder_id: "f1" }),
        session("c", { pinned: true }),
        session("d"),
      ],
      byPinned,
    );

    expect(pinned.map((s) => s.id)).toEqual(["a", "c"]);
    expect(unpinned.map((s) => s.id)).toEqual(["b", "d"]);
  });

  it("keeps the incoming order inside each side and never duplicates a row", () => {
    const input = [
      session("1"),
      session("2", { pinned: true }),
      session("3", { pinned: true }),
      session("4"),
    ];
    const { matched: pinned, rest: unpinned } = partitionSessions(input, byPinned);

    expect(pinned.map((s) => s.id)).toEqual(["2", "3"]);
    expect(unpinned.map((s) => s.id)).toEqual(["1", "4"]);
    expect(pinned.length + unpinned.length).toBe(input.length);
  });

  it("returns an empty pinned group when nothing is pinned", () => {
    const { matched: pinned, rest: unpinned } = partitionSessions(
      [session("x"), session("y")],
      byPinned,
    );
    expect(pinned).toEqual([]);
    expect(unpinned).toHaveLength(2);
  });
});
