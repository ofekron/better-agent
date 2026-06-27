import { describe, it, expect, beforeEach, vi } from "vitest";

import { eventBus } from "../src/lib/eventBus";
import {
  sessionRegistry,
  statusRankOf,
  statusRankForRow,
} from "../src/lib/sessionRegistry";

/**
 * Reducer-level tests for the singleton sessionRegistry.
 *
 * The registry is the single source of truth that powers
 * `<SessionStatusBadge>` and `<ProjectStatusBadge>` everywhere they
 * render. These tests pin its delta-application semantics.
 *
 * INVARIANTs locked here:
 *  - Sessions enter the map ONLY via bootstrap, `session_created`, or
 *    `session_metadata_updated`. Running/unread deltas for unknown
 *    sids are silently dropped (no phantom-entry inflation of
 *    aggregates).
 *  - Per-project aggregates are derived locally by summing visible
 *    sessions. Hidden sessions (cwd === "") never contribute.
 *  - Bootstrap races: deltas arriving before the first successful
 *    bootstrap are buffered FIFO and drained after the snapshot.
 *
 * The bootstrap path goes through `fetch` — we stub `globalThis.fetch`
 * per test to seed the registry from a controlled "REST snapshot".
 */

type SessionRow = {
  id: string;
  cwd?: string;
  node_id?: string;
  is_running?: boolean;
  unread_count?: number;
};

function stubSessionsResponse(sessions: SessionRow[]) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ sessions }),
  });
  (globalThis as unknown as { fetch: typeof fetch }).fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

async function bootstrapWith(sessions: SessionRow[]) {
  stubSessionsResponse(sessions);
  await sessionRegistry.bootstrap();
}

/** Re-create a fresh registry surface between tests by re-binding the
 * bus (drops the old subscriptions) and seeding with an empty
 * bootstrap snapshot — equivalent to a fresh page load. The module-
 * level singleton itself can't be reset, but bootstrap REPLACES its
 * `sessions` / `projects` maps with the new snapshot. */
async function resetRegistry() {
  (sessionRegistry as unknown as { __resetForTests: () => void }).__resetForTests();
  sessionRegistry.bind();
  await bootstrapWith([]);
}

function resetForBootstrapTests() {
  (sessionRegistry as unknown as { __resetForTests: () => void }).__resetForTests();
  sessionRegistry.bind();
}

describe("sessionRegistry — per-session deltas", () => {
  beforeEach(async () => {
    await resetRegistry();
  });

  it("session_running_changed flips is_running for the matching sid", () => {
    const sid = "sess-running-1";
    eventBus.publish("session_created", {
      session: { id: sid, cwd: "/p", node_id: "primary" },
    });
    expect(sessionRegistry.getSession(sid).is_running).toBe(false);
    eventBus.publish("session_running_changed", {
      session_id: sid,
      value: true,
      cwd: "/p",
      node_id: "primary",
    });
    expect(sessionRegistry.getSession(sid).is_running).toBe(true);
    eventBus.publish("session_running_changed", {
      session_id: sid,
      value: false,
      cwd: "/p",
      node_id: "primary",
    });
    expect(sessionRegistry.getSession(sid).is_running).toBe(false);
  });

  it("testape_session_state updates testape_active for the matching sid", () => {
    const sid = "sess-testape-1";
    eventBus.publish("session_created", {
      session: { id: sid, cwd: "/p", node_id: "primary" },
    });
    expect(sessionRegistry.getSession(sid).testape_active).toBe(false);
    eventBus.publish("testape_session_state", {
      session_id: sid,
      active: true,
    });
    expect(sessionRegistry.getSession(sid).testape_active).toBe(true);
    eventBus.publish("testape_session_state", {
      session_id: sid,
      active: false,
    });
    expect(sessionRegistry.getSession(sid).testape_active).toBe(false);
  });

  it("session_unread_changed updates unread_count", () => {
    const sid = "sess-unread-1";
    eventBus.publish("session_created", {
      session: { id: sid, cwd: "/p", node_id: "primary" },
    });
    eventBus.publish("session_unread_changed", {
      session_id: sid,
      unread_count: 7,
      cwd: "/p",
      node_id: "primary",
    });
    expect(sessionRegistry.getSession(sid).unread_count).toBe(7);
    eventBus.publish("session_unread_changed", {
      session_id: sid,
      unread_count: 0,
      cwd: "/p",
      node_id: "primary",
    });
    expect(sessionRegistry.getSession(sid).unread_count).toBe(0);
  });

  it("session_deleted drops the sid's cached meta", () => {
    const sid = "sess-doomed";
    eventBus.publish("session_created", {
      session: { id: sid, cwd: "/p", node_id: "primary", is_running: true, unread_count: 3 },
    });
    expect(sessionRegistry.getSession(sid).is_running).toBe(true);
    eventBus.publish("session_deleted", { session_id: sid });
    expect(sessionRegistry.getSession(sid).is_running).toBe(false);
    expect(sessionRegistry.getSession(sid).unread_count).toBe(0);
  });

  it("unknown sid returns the stable EMPTY_SESSION sentinel", () => {
    const a = sessionRegistry.getSession("never-touched-a");
    const b = sessionRegistry.getSession("never-touched-b");
    expect(a).toBe(b);
    expect(a).toEqual({
      is_running: false,
      unread_count: 0,
      monitoring_state: "stopped",
      markers: {},
      testape_active: false,
    });
  });

  it("per-sid subscriber fires only on its own slice", () => {
    const a = "sess-A";
    const b = "sess-B";
    eventBus.publish("session_created", { session: { id: a, cwd: "/p" } });
    eventBus.publish("session_created", { session: { id: b, cwd: "/p" } });
    let aFires = 0;
    let bFires = 0;
    const offA = sessionRegistry.subscribeSession(a, () => aFires++);
    const offB = sessionRegistry.subscribeSession(b, () => bFires++);
    eventBus.publish("session_unread_changed", {
      session_id: a,
      unread_count: 1,
      cwd: "/p",
    });
    expect(aFires).toBe(1);
    expect(bFires).toBe(0);
    eventBus.publish("session_unread_changed", {
      session_id: b,
      unread_count: 1,
      cwd: "/p",
    });
    expect(aFires).toBe(1);
    expect(bFires).toBe(1);
    offA();
    offB();
  });

  it("no-op updates (same slice) do not refire subscribers", () => {
    const sid = "sess-stable";
    eventBus.publish("session_created", { session: { id: sid, cwd: "/p" } });
    eventBus.publish("session_unread_changed", {
      session_id: sid,
      unread_count: 4,
      cwd: "/p",
    });
    let fires = 0;
    const off = sessionRegistry.subscribeSession(sid, () => fires++);
    eventBus.publish("session_unread_changed", {
      session_id: sid,
      unread_count: 4,
      cwd: "/p",
    });
    expect(fires).toBe(0);
    eventBus.publish("session_unread_changed", {
      session_id: sid,
      unread_count: 5,
      cwd: "/p",
    });
    expect(fires).toBe(1);
    off();
  });
});

describe("sessionRegistry — auto-insert vs hidden-drop", () => {
  beforeEach(async () => {
    await resetRegistry();
  });

  it("hidden delta (cwd === '') for an unknown sid is dropped (no phantom)", () => {
    eventBus.publish("session_running_changed", {
      session_id: "hidden-ghost",
      value: true,
      cwd: "",
      node_id: "primary",
    });
    expect(sessionRegistry.getSession("hidden-ghost").is_running).toBe(false);
    // No aggregate inflation under any project.
    expect(sessionRegistry.getProject("/p", "primary").running_count).toBe(0);
  });

  it("visible delta for an unknown sid auto-inserts (covers working_mode-cleared flip)", () => {
    // Backend's `session_created` is gated on working_mode — so a
    // session that's created WITH working_mode, then later flipped
    // to visible, never fires `session_created`. Its first
    // visible-mode signal is a `running_changed` with real cwd; we
    // materialize from the payload.
    eventBus.publish("session_running_changed", {
      session_id: "late-arriver",
      value: true,
      cwd: "/p",
      node_id: "primary",
    });
    expect(sessionRegistry.getSession("late-arriver").is_running).toBe(true);
    expect(sessionRegistry.getProject("/p", "primary").running_count).toBe(1);
  });

  it("visible unread delta for an unknown sid auto-inserts", () => {
    eventBus.publish("session_unread_changed", {
      session_id: "late-arriver-2",
      unread_count: 7,
      cwd: "/p",
      node_id: "primary",
    });
    expect(sessionRegistry.getSession("late-arriver-2").unread_count).toBe(7);
    expect(sessionRegistry.getProject("/p", "primary").unread_session_count).toBe(1);
  });

  it("visibility flip (visible → hidden via cwd='') removes from aggregate", async () => {
    await bootstrapWith([
      { id: "flipper", cwd: "/p", is_running: true, unread_count: 3 },
    ]);
    expect(sessionRegistry.getProject("/p", "primary")).toEqual({
      running_count: 1,
      unread_session_count: 1,
    });
    // Visibility flips to hidden: backend ships cwd="" — we honor it.
    eventBus.publish("session_unread_changed", {
      session_id: "flipper",
      unread_count: 4,
      cwd: "",
      node_id: "primary",
    });
    expect(sessionRegistry.getProject("/p", "primary")).toEqual({
      running_count: 0,
      unread_session_count: 0,
    });
    // Per-session state still applies — chat view may still consume it.
    expect(sessionRegistry.getSession("flipper").unread_count).toBe(4);
  });
});

describe("sessionRegistry — project aggregates", () => {
  beforeEach(async () => {
    await resetRegistry();
  });

  it("derives running_count + unread_session_count from bootstrap snapshot", async () => {
    await bootstrapWith([
      { id: "s1", cwd: "/p", node_id: "primary", is_running: true, unread_count: 2 },
      { id: "s2", cwd: "/p", node_id: "primary", is_running: false, unread_count: 3 },
      { id: "s3", cwd: "/q", node_id: "primary", is_running: true, unread_count: 1 },
    ]);
    expect(sessionRegistry.getProject("/p", "primary")).toEqual({
      running_count: 1,
      unread_session_count: 2,
    });
    expect(sessionRegistry.getProject("/q", "primary")).toEqual({
      running_count: 1,
      unread_session_count: 1,
    });
  });

  it("hidden session (cwd === '') does NOT contribute to any aggregate", () => {
    eventBus.publish("session_created", {
      session: {
        id: "hidden-eng",
        cwd: "",
        node_id: "primary",
        is_running: true,
        unread_count: 5,
      },
    });
    // No project key for "" — nothing leaks into any bucket.
    expect(sessionRegistry.getProject("", "primary")).toEqual({
      running_count: 0,
      unread_session_count: 0,
    });
  });

  it("running delta with cwd=='' updates per-session but not aggregate", async () => {
    await bootstrapWith([
      { id: "shown", cwd: "/p", is_running: true, unread_count: 0 },
      { id: "hidden", cwd: "", is_running: false, unread_count: 0 },
    ]);
    expect(sessionRegistry.getProject("/p", "primary").running_count).toBe(1);
    // Hidden session flips running. cwd:"" signals "skip aggregate".
    eventBus.publish("session_running_changed", {
      session_id: "hidden",
      value: true,
      cwd: "",
      node_id: "primary",
    });
    expect(sessionRegistry.getSession("hidden").is_running).toBe(true);
    // Aggregate unchanged.
    expect(sessionRegistry.getProject("/p", "primary").running_count).toBe(1);
  });

  it("session_deleted recomputes aggregate without the deleted session", async () => {
    await bootstrapWith([
      { id: "a", cwd: "/p", is_running: true, unread_count: 2 },
      { id: "b", cwd: "/p", is_running: true, unread_count: 3 },
    ]);
    expect(sessionRegistry.getProject("/p", "primary")).toEqual({
      running_count: 2,
      unread_session_count: 2,
    });
    eventBus.publish("session_deleted", { session_id: "a" });
    expect(sessionRegistry.getProject("/p", "primary")).toEqual({
      running_count: 1,
      unread_session_count: 1,
    });
  });

  it("session_metadata_updated.patch.cwd migrates aggregate", async () => {
    await bootstrapWith([
      { id: "mover", cwd: "/p", is_running: true, unread_count: 4 },
    ]);
    expect(sessionRegistry.getProject("/p", "primary")).toEqual({
      running_count: 1,
      unread_session_count: 1,
    });
    eventBus.publish("session_metadata_updated", {
      session_id: "mover",
      patch: { cwd: "/q" },
    });
    expect(sessionRegistry.getProject("/p", "primary")).toEqual({
      running_count: 0,
      unread_session_count: 0,
    });
    expect(sessionRegistry.getProject("/q", "primary")).toEqual({
      running_count: 1,
      unread_session_count: 1,
    });
  });

  it("testape_active counts toward running_count even when monitoring_state is stopped", async () => {
    // A session with a TestApe run active (but no agent turn in flight)
    // is "running in testape" — the project badge must show 1.
    await bootstrapWith([
      { id: "ta", cwd: "/p", node_id: "primary", is_running: false, unread_count: 0 },
    ]);
    expect(sessionRegistry.getProject("/p", "primary").running_count).toBe(0);
    eventBus.publish("testape_session_state", { session_id: "ta", active: true });
    expect(sessionRegistry.getProject("/p", "primary").running_count).toBe(1);
    eventBus.publish("testape_session_state", { session_id: "ta", active: false });
    expect(sessionRegistry.getProject("/p", "primary").running_count).toBe(0);
  });

  it("session_created is idempotent — second created for same sid is a no-op", () => {
    eventBus.publish("session_created", {
      session: { id: "dup", cwd: "/p", is_running: true, unread_count: 2 },
    });
    eventBus.publish("session_created", {
      session: { id: "dup", cwd: "/p", is_running: true, unread_count: 99 },
    });
    expect(sessionRegistry.getSession("dup").unread_count).toBe(2);
    expect(sessionRegistry.getProject("/p", "primary")).toEqual({
      running_count: 1,
      unread_session_count: 1,
    });
  });
});

describe("sessionRegistry — bootstrap mechanics", () => {
  beforeEach(() => {
    resetForBootstrapTests();
  });

  it("deltas before first successful bootstrap are buffered then drained FIFO", async () => {
    // No bootstrap yet — these deltas land in the buffer.
    eventBus.publish("session_created", {
      session: { id: "buf-1", cwd: "/p", is_running: false, unread_count: 0 },
    });
    eventBus.publish("session_unread_changed", {
      session_id: "buf-1",
      unread_count: 9,
      cwd: "/p",
    });
    // Before bootstrap, the buffered delta isn't applied yet.
    // (The first event created the session but `_bootstrapped` is
    // still false, so even the snapshot/projects derivation hasn't
    // happened — getSession returns the EMPTY sentinel.)
    expect(sessionRegistry.getSession("buf-1").unread_count).toBe(0);
    // Snapshot is empty; bootstrap drains the buffer in order.
    await bootstrapWith([]);
    expect(sessionRegistry.getSession("buf-1").unread_count).toBe(9);
    expect(sessionRegistry.getProject("/p", "primary").unread_session_count).toBe(1);
  });

  it("concurrent bootstrap calls share one in-flight promise", async () => {
    let resolved = 0;
    const fetchMock = vi.fn().mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolved += 1;
          setTimeout(
            () =>
              resolve({
                ok: true,
                json: async () => ({ sessions: [] }),
              } as Response),
            10,
          );
        }),
    );
    (globalThis as unknown as { fetch: typeof fetch }).fetch =
      fetchMock as unknown as typeof fetch;
    const a = sessionRegistry.bootstrap();
    const b = sessionRegistry.bootstrap();
    expect(a).toBe(b); // same promise — dedup
    await a;
    expect(resolved).toBe(1); // fetch called exactly once
  });

  it("failed bootstrap keeps _bootstrapped=false; buffer survives until success", async () => {
    eventBus.publish("session_created", {
      session: { id: "preboot", cwd: "/p", is_running: true, unread_count: 0 },
    });
    // Reject the first fetch.
    (globalThis as unknown as { fetch: typeof fetch }).fetch = vi
      .fn()
      .mockRejectedValueOnce(new Error("network")) as unknown as typeof fetch;
    await sessionRegistry.bootstrap();
    // Session NOT yet in map — bootstrap didn't run drain.
    expect(sessionRegistry.getSession("preboot").is_running).toBe(false);
    // Second attempt succeeds — drain happens.
    await bootstrapWith([]);
    expect(sessionRegistry.getSession("preboot").is_running).toBe(true);
    expect(sessionRegistry.getProject("/p", "primary").running_count).toBe(1);
  });
});

describe("status rank (mirror of backend _session_status_rank)", () => {
  const m = (tag: string) => ({ ext: { color: "#x", tooltip: "t", tag } });

  it("buckets: 4 running, 3 needs, 2 new, 1 done, 0 none — highest wins", () => {
    expect(statusRankOf({ monitoring_state: "active" })).toBe(4);
    expect(statusRankOf({ monitoring_state: "waiting_on_background" })).toBe(4);
    expect(statusRankOf({ monitoring_state: "blocked_on_user" })).toBe(3);
    expect(statusRankOf({ monitoring_state: "idle", markers: m("NEEDS_USER_DECISION") })).toBe(3);
    expect(statusRankOf({ unread_count: 2 })).toBe(2);
    expect(statusRankOf({ markers: m("ALL_TASKS__DONE") })).toBe(1);
    expect(statusRankOf({ monitoring_state: "idle" })).toBe(0);
    // precedence: running outranks a stale needs-decision marker
    expect(statusRankOf({ monitoring_state: "active", markers: m("NEEDS_USER_DECISION") })).toBe(4);
    // classification by TAG, not color — untagged marker is inert
    expect(statusRankOf({ markers: { ext: { color: "#d29922", tooltip: "x" } } })).toBe(0);
  });

  it("statusRankForRow prefers the live registry over the row snapshot", async () => {
    await resetRegistry();
    const sid = "rank-live";
    eventBus.publish("session_created", { session: { id: sid, cwd: "/p", node_id: "primary" } });
    eventBus.publish("session_monitoring_changed", {
      session_id: sid,
      monitoring_state: "active",
      cwd: "/p",
      node_id: "primary",
    });
    // Row snapshot claims stopped, but the live registry says active → live wins.
    expect(statusRankForRow({ id: sid, monitoring_state: "stopped" })).toBe(4);
  });

  it("statusRankForRow falls back to row fields when the sid is unseeded", async () => {
    await resetRegistry();
    expect(statusRankForRow({ id: "deep-page", monitoring_state: "active" })).toBe(4);
    expect(statusRankForRow({ id: "deep-page-2", unread_count: 3 })).toBe(2);
    expect(statusRankForRow({ id: "deep-page-3" })).toBe(0);
  });

  it("seedFromRows fills missing sids without clobbering fresher live state", async () => {
    await resetRegistry();
    const sid = "seed-1";
    eventBus.publish("session_created", { session: { id: sid, cwd: "/p", node_id: "primary" } });
    eventBus.publish("session_monitoring_changed", {
      session_id: sid,
      monitoring_state: "active",
      cwd: "/p",
      node_id: "primary",
    });
    // A staler page row for the SAME sid must NOT downgrade the live entry…
    sessionRegistry.seedFromRows([
      { id: sid, monitoring_state: "stopped", cwd: "/p", node_id: "primary" },
      { id: "seed-new", monitoring_state: "active", cwd: "/p", node_id: "primary" },
    ]);
    expect(sessionRegistry.getSession(sid).is_running).toBe(true);
    // …but a brand-new sid IS materialized from the page row.
    expect(sessionRegistry.getSession("seed-new").is_running).toBe(true);
  });

  // Regression for #185: <SessionStatusBadge> → useSessionMeta →
  // useSyncExternalStore infinite-looped ("getSnapshot should be cached").
  // applyRoutedDelta's update path dropped `testape_active`, leaving it
  // undefined; getSession cached `!!undefined` (false) but compared it
  // against the raw `undefined` on the next call, so the cache missed
  // every time and getSnapshot returned a fresh object each render.
  it("getSession returns a stable reference after a routed delta (#185)", async () => {
    await resetRegistry();
    const sid = "sess-cache-invariant";
    eventBus.publish("session_created", {
      session: { id: sid, cwd: "/p", node_id: "primary" },
    });
    // session_unread_changed routes through applyRoutedDelta's update
    // path — the one that used to drop testape_active.
    eventBus.publish("session_unread_changed", {
      session_id: sid,
      unread_count: 3,
      cwd: "/p",
      node_id: "primary",
    });
    const a = sessionRegistry.getSession(sid);
    const b = sessionRegistry.getSession(sid);
    expect(a).toBe(b); // SAME reference — cache invariant holds
    expect(a.testape_active).toBe(false);
  });
});
