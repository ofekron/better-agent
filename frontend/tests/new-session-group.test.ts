import { describe, expect, it } from "vitest";
import { isNewSession, partitionSessions } from "../src/lib/sessionSort";
import type { Session } from "../src/types";

function session(id: string, extra: Partial<Session> = {}): Session {
  return { id, name: id, ...extra } as Session;
}

describe("new session grouping", () => {
  it("keeps sessions with history OUT of the New group", () => {
    // Regression: the New group used to swallow every unpinned/unfiled
    // session regardless of age or content.
    const { matched, rest } = partitionSessions(
      [
        session("old", { message_count: 42 }),
        session("fresh", { message_count: 0 }),
        session("also-old", { message_count: 1 }),
      ],
      isNewSession,
    );

    expect(matched.map((s) => s.id)).toEqual(["fresh"]);
    expect(rest.map((s) => s.id)).toEqual(["old", "also-old"]);
  });

  it("treats only empty, unpinned, unfiled sessions as new", () => {
    expect(isNewSession(session("a", { message_count: 0 }))).toBe(true);
    expect(isNewSession(session("b"))).toBe(true); // missing count = 0
    expect(isNewSession(session("c", { message_count: 3 }))).toBe(false);
    expect(isNewSession(session("d", { message_count: 0, pinned: true }))).toBe(
      false,
    );
    expect(
      isNewSession(session("e", { message_count: 0, folder_id: "f1" })),
    ).toBe(false);
  });

  it("preserves incoming order on both sides and never duplicates", () => {
    const input = [
      session("1", { message_count: 2 }),
      session("2", { message_count: 0 }),
      session("3", { message_count: 0 }),
      session("4", { message_count: 9 }),
    ];
    const { matched, rest } = partitionSessions(input, isNewSession);

    expect(matched.map((s) => s.id)).toEqual(["2", "3"]);
    expect(rest.map((s) => s.id)).toEqual(["1", "4"]);
    expect(matched.length + rest.length).toBe(input.length);
  });
});
