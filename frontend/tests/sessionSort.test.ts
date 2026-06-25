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
      mk({ id: "no-prompt", updated_at: "2026-06-10T00:00:00Z", message_count: 1 }),
      mk({
        id: "has-prompt",
        updated_at: "2026-01-01T00:00:00Z",
        last_user_prompt_at: "2026-06-01T00:00:00Z",
        message_count: 1,
      } as Partial<Session> & Pick<Session, "id">),
    ];
    const out = sortSessionsForList(sessions, false, "last_user_prompt_at");
    expect(out.map((s) => s.id)).toEqual(["has-prompt", "no-prompt"]);
  });

  it("orders by last_opened_at when that is the sort field", () => {
    const sessions = [
      mk({
        id: "opened-older",
        updated_at: "2026-06-05T00:00:00Z",
        last_opened_at: "2026-06-01T00:00:00Z",
        message_count: 1,
      } as Partial<Session> & Pick<Session, "id">),
      mk({
        id: "opened-newer",
        updated_at: "2026-06-02T00:00:00Z",
        last_opened_at: "2026-06-04T00:00:00Z",
        message_count: 1,
      } as Partial<Session> & Pick<Session, "id">),
    ];
    const out = sortSessionsForList(sessions, false, "last_opened_at");
    expect(out.map((s) => s.id)).toEqual(["opened-newer", "opened-older"]);
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

  describe("rankOf seam (status sort)", () => {
    const rankById: Record<string, number> = { err: 6, decide: 5, neww: 4, todo: 3, run: 2, done: 1, idle: 0 };
    const rankOf = (s: Session) => rankById[s.id] ?? 0;

    it("no rankOf → byte-identical to the time-only sort", () => {
      const sessions = [
        mk({ id: "old", updated_at: "2026-01-01T00:00:00Z" }),
        mk({ id: "new", updated_at: "2026-06-01T00:00:00Z" }),
      ];
      const withUndefined = sortSessionsForList(sessions, false, "updated_at", undefined);
      const baseline = sortSessionsForList(sessions, false, "updated_at");
      expect(withUndefined.map((s) => s.id)).toEqual(baseline.map((s) => s.id));
    });

    it("status is the strongest key below empty-new + pinned", () => {
      const sessions = [
        // idle but far newer
        mk({ id: "idle", updated_at: "2030-01-01T00:00:00Z", message_count: 5 } as Partial<Session> & Pick<Session, "id">),
        // needs-decision, mid
        mk({ id: "decide", updated_at: "2024-01-01T00:00:00Z", message_count: 5 } as Partial<Session> & Pick<Session, "id">),
        // running but old
        mk({ id: "run", updated_at: "2020-01-01T00:00:00Z", message_count: 5 } as Partial<Session> & Pick<Session, "id">),
      ];
      const out = sortSessionsForList(sessions, false, "updated_at", rankOf);
      expect(out.map((s) => s.id)).toEqual(["decide", "run", "idle"]);
    });

    it("pinned beats status; empty-new beats both", () => {
      const sessions = [
        mk({ id: "run", updated_at: "2020-01-01T00:00:00Z", message_count: 5 } as Partial<Session> & Pick<Session, "id">),
        mk({ id: "idle", pinned: true, updated_at: "2020-01-01T00:00:00Z", message_count: 5 } as Partial<Session> & Pick<Session, "id">),
        mk({ id: "empty", updated_at: "2020-01-01T00:00:00Z", message_count: 0 } as Partial<Session> & Pick<Session, "id">),
      ];
      const out = sortSessionsForList(sessions, false, "updated_at", rankOf);
      // empty-new first, then pinned-idle, then running
      expect(out.map((s) => s.id)).toEqual(["empty", "idle", "run"]);
    });
  });
});
