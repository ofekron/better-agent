import { describe, expect, it } from "vitest";
import { sortSessionsForList } from "../src/lib/sessionSort";
import type { Session } from "../src/types";

function session(
  id: string,
  updatedAt: string,
  extra: Partial<Session> = {},
): Session {
  return {
    id,
    name: id,
    model: "model",
    cwd: "/repo",
    created_at: updatedAt,
    updated_at: updatedAt,
    messages: [],
    ...extra,
  };
}

describe("sortSessionsForList", () => {
  it("resorts fetched sessions when modified time changes", () => {
    const oldSession = session("old", "2026-01-01T00:00:00.000Z");
    const newerSession = session("newer", "2026-01-02T00:00:00.000Z");

    const updated = {
      ...oldSession,
      updated_at: "2026-01-03T00:00:00.000Z",
    };

    expect(sortSessionsForList([updated, newerSession]).map((s) => s.id)).toEqual([
      "old",
      "newer",
    ]);
  });

  it("keeps pinned sessions above newer unpinned sessions", () => {
    const pinned = session("pinned", "2026-01-01T00:00:00.000Z", {
      pinned: true,
    });
    const newer = session("newer", "2026-01-02T00:00:00.000Z");

    expect(sortSessionsForList([newer, pinned]).map((s) => s.id)).toEqual([
      "pinned",
      "newer",
    ]);
  });
});
