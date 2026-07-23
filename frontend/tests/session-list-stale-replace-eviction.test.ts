import { describe, expect, it } from "vitest";
import { preservedLocalSessionsForReplace } from "../src/hooks/useSession";
import type { Session } from "../src/types";

function mk(over: Partial<Session> & Pick<Session, "id">): Session {
  return { name: over.id, ...over } as Session;
}

/** Replicates the PRE-FIX replace-merge preservation rule (offline-only)
 * so the regression case can assert the exact row the old code dropped. */
function oldPreserve(prev: Session[], pageIds: Set<string>): Session[] {
  return prev.filter((s) => s.offline_pending && !pageIds.has(s.id));
}

describe("preservedLocalSessionsForReplace (stale replace-list eviction)", () => {
  it("preserves a server-confirmed session inserted AFTER a stale replace fetch dispatched", () => {
    // The Z.AI repro: a `/api/sessions` replace fetch is dispatched at
    // gen=0, the user creates a session (inserted at gen=1), then the
    // pre-creation page resolves without it. It must survive.
    const zai = mk({ id: "zai", offline_pending: false });
    const prev = [zai, mk({ id: "existing" })];
    const page = new Set(["existing"]); // pre-creation snapshot
    const insertGenById = new Map([["zai", 1]]);

    const preserved = preservedLocalSessionsForReplace(prev, page, {
      dispatchInsertGen: 0,
      insertGenById,
    });

    expect(preserved.map((s) => s.id)).toContain("zai");
    // Regression proof: the old offline-only rule would have dropped it.
    expect(oldPreserve(prev, page).map((s) => s.id)).not.toContain("zai");
  });

  it("evicts a session genuinely removed on the backend (inserted BEFORE dispatch)", () => {
    const stale = mk({ id: "gone", offline_pending: false });
    const prev = [stale, mk({ id: "keep" })];
    const page = new Set(["keep"]); // backend really removed 'gone'
    // 'gone' was inserted at gen=1 but the current fetch dispatched at gen=2
    // (i.e. after the insert was already acknowledged/known).
    const insertGenById = new Map([["gone", 1]]);

    const preserved = preservedLocalSessionsForReplace(prev, page, {
      dispatchInsertGen: 2,
      insertGenById,
    });

    expect(preserved.map((s) => s.id)).not.toContain("gone");
  });

  it("always preserves unacknowledged offline sessions regardless of generation", () => {
    const offline = mk({ id: "offline", offline_pending: true });
    const prev = [offline];
    const page = new Set<string>([]);

    const preserved = preservedLocalSessionsForReplace(prev, page, {
      dispatchInsertGen: 99,
      insertGenById: new Map(),
    });

    expect(preserved.map((s) => s.id)).toEqual(["offline"]);
  });

  it("does not re-preserve sessions already present in the page (no duplication)", () => {
    const s = mk({ id: "dup", offline_pending: true });
    const prev = [s];
    const page = new Set(["dup"]);

    const preserved = preservedLocalSessionsForReplace(prev, page, {
      dispatchInsertGen: 0,
      insertGenById: new Map([["dup", 5]]),
    });

    expect(preserved).toEqual([]);
  });

  it("evicts an untracked non-offline row absent from the page (no false retention)", () => {
    const prev = [mk({ id: "orphan", offline_pending: false })];
    const page = new Set<string>([]);

    const preserved = preservedLocalSessionsForReplace(prev, page, {
      dispatchInsertGen: 0,
      insertGenById: new Map(),
    });

    expect(preserved).toEqual([]);
  });
});
