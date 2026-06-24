import { describe, expect, it } from "vitest";
import type { Session } from "../src/types";
import { sortSessionsForList } from "../src/lib/sessionSort";

function mk(over: Partial<Session> & Pick<Session, "id">): Session {
  return { name: over.id, ...over } as Session;
}

describe("sortSessionsForList", () => {
  it("orders by updated_at when that is the sort field", () => {
    const sessions = [
      mk({ id: "old", updated_at: "2026-01-01T00:00:00Z" }),
      mk({ id: "new", updated_at: "2026-06-01T00:00:00Z" }),
    ];
    const out = sortSessionsForList(sessions, false, "updated_at");
    expect(out.map((s) => s.id)).toEqual(["new", "old"]);
  });

  it("orders by last_user_prompt_at, NOT updated_at", () => {
    // Regression: the comparator used to hardcode updated_at, so a
    // session whose last prompt is newer but updated_at is older
    // landed in the wrong spot — scrambling the backend's ordering
    // on every local mutation.
    const sessions = [
      mk({
        id: "prompt-older",
        updated_at: "2026-06-05T00:00:00Z",
        last_user_prompt_at: "2026-06-01T00:00:00Z",
      } as Partial<Session> & Pick<Session, "id">),
      mk({
        id: "prompt-newer",
        updated_at: "2026-06-02T00:00:00Z",
        last_user_prompt_at: "2026-06-04T00:00:00Z",
      } as Partial<Session> & Pick<Session, "id">),
    ];
    const out = sortSessionsForList(sessions, false, "last_user_prompt_at");
    expect(out.map((s) => s.id)).toEqual(["prompt-newer", "prompt-older"]);
  });

  it("sessions missing the sort field sink to the bottom", () => {
    const sessions = [
      mk({ id: "no-prompt", updated_at: "2026-06-10T00:00:00Z" }),
      mk({
        id: "has-prompt",
        updated_at: "2026-01-01T00:00:00Z",
        last_user_prompt_at: "2026-06-01T00:00:00Z",
      } as Partial<Session> & Pick<Session, "id">),
    ];
    const out = sortSessionsForList(sessions, false, "last_user_prompt_at");
    expect(out.map((s) => s.id)).toEqual(["has-prompt", "no-prompt"]);
  });

  it("pinned always wins over the sort field", () => {
    const sessions = [
      mk({
        id: "fresh",
        pinned: false,
        last_user_prompt_at: "2026-06-10T00:00:00Z",
      } as Partial<Session> & Pick<Session, "id">),
      mk({
        id: "pinned",
        pinned: true,
        last_user_prompt_at: "2026-01-01T00:00:00Z",
      } as Partial<Session> & Pick<Session, "id">),
    ];
    const out = sortSessionsForList(sessions, false, "last_user_prompt_at");
    expect(out[0].id).toBe("pinned");
  });
});
