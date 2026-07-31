import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { eventBus } from "../src/lib/eventBus";
import {
  sessionRegistry,
  statusRankOf,
  statusRankForRow,
  useSessionMeta,
  useProjectAggregate,
  ackSessionSeen,
  markSessionUnread,
} from "../src/lib/sessionRegistry";

/**
 * Reducer-level tests for the singleton sessionRegistry.
 *
 * The registry is the single source of truth that powers
 * `<SessionStatusBadge>` and `<ProjectStatusBadge>` everywhere they
 * render. These tests pin its delta-application semantics.
 *
 * INVARIANTs locked here:
 *  - Sessions enter the map via a `/api/sessions` page merge,
 *    `session_created`, or a routed delta carrying a visible cwd.
 *    Running/unread deltas for unknown sids with an EMPTY cwd are
 *    silently dropped (no phantom-entry inflation of aggregates).
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
  pending_user_input_count?: number;
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

  it("turn_start marks a seeded file-editing session running before run_state", () => {
    const sid = "file-edit-running";
    eventBus.publish("session_created", {
      session: { id: sid, cwd: "/p", node_id: "primary" },
    });

    eventBus.publish("turn_start", { app_session_id: sid });

    expect(sessionRegistry.getSession(sid).is_running).toBe(true);
    expect(statusRankForRow({ id: sid, monitoring_state: "stopped" })).toBe(2);
  });

  it("turn_start does not materialize unknown sessions", () => {
    eventBus.publish("turn_start", { app_session_id: "unknown-file-edit" });

    expect(sessionRegistry.getSession("unknown-file-edit").is_running).toBe(false);
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

  it("session_user_input_changed updates pending input count", () => {
    const sid = "sess-input-1";
    eventBus.publish("session_created", {
      session: { id: sid, cwd: "/p", node_id: "primary" },
    });
    eventBus.publish("session_user_input_changed", {
      session_id: sid,
      pending_user_input_count: 2,
    });
    expect(sessionRegistry.getSession(sid).pending_user_input_count).toBe(2);
    eventBus.publish("session_user_input_changed", {
      session_id: sid,
      pending_user_input_count: 0,
    });
    expect(sessionRegistry.getSession(sid).pending_user_input_count).toBe(0);
  });

  it("turn_start clears the live error indication for that session", () => {
    const sid = "sess-error-clear";
    eventBus.publish("session_created", {
      session: { id: sid, cwd: "/p", node_id: "primary" },
    });
    eventBus.publish("session_error_changed", {
      session_id: sid,
      has_error: true,
      cwd: "/p",
      node_id: "primary",
    });
    expect(sessionRegistry.getSession(sid).has_error).toBe(true);
    expect(statusRankForRow({ id: sid, monitoring_state: "stopped" })).toBe(6);

    eventBus.publish("turn_start", { app_session_id: sid });

    expect(sessionRegistry.getSession(sid).has_error).toBe(false);
    expect(statusRankForRow({ id: sid, monitoring_state: "stopped" })).toBe(2);
    eventBus.publish("run_state", { app_session_id: sid, runs: [] });
    expect(statusRankForRow({ id: sid, monitoring_state: "stopped" })).toBe(0);
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
      pending_user_input_count: 0,
      monitoring_state: "stopped",
      markers: {},
      testape_active: false,
      has_error: false,
      current_todos: [],
      current_tasks: [],
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
      waiting_for_user_count: 0,
      errored_count: 0,
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
      waiting_for_user_count: 0,
      errored_count: 0,
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
      waiting_for_user_count: 0,
      errored_count: 0,
    });
    expect(sessionRegistry.getProject("/q", "primary")).toEqual({
      running_count: 1,
      unread_session_count: 1,
      waiting_for_user_count: 0,
      errored_count: 0,
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
      waiting_for_user_count: 0,
      errored_count: 0,
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
      waiting_for_user_count: 0,
      errored_count: 0,
    });
    eventBus.publish("session_deleted", { session_id: "a" });
    expect(sessionRegistry.getProject("/p", "primary")).toEqual({
      running_count: 1,
      unread_session_count: 1,
      waiting_for_user_count: 0,
      errored_count: 0,
    });
  });

  it("session_metadata_updated.patch.cwd migrates aggregate", async () => {
    await bootstrapWith([
      { id: "mover", cwd: "/p", is_running: true, unread_count: 4 },
    ]);
    expect(sessionRegistry.getProject("/p", "primary")).toEqual({
      running_count: 1,
      unread_session_count: 1,
      waiting_for_user_count: 0,
      errored_count: 0,
    });
    eventBus.publish("session_metadata_updated", {
      session_id: "mover",
      patch: { cwd: "/q" },
    });
    expect(sessionRegistry.getProject("/p", "primary")).toEqual({
      running_count: 0,
      unread_session_count: 0,
      waiting_for_user_count: 0,
      errored_count: 0,
    });
    expect(sessionRegistry.getProject("/q", "primary")).toEqual({
      running_count: 1,
      unread_session_count: 1,
      waiting_for_user_count: 0,
      errored_count: 0,
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
      waiting_for_user_count: 0,
      errored_count: 0,
    });
  });
});

/**
 * Every status dimension gets its own project counter, and every
 * dimension mutation must move it. Before these, only running/unread
 * were aggregated and only running/unread deltas triggered a project
 * recompute — so an error or a waiting-for-user change left the project
 * badge stale until an unrelated delta happened to recompute it.
 */
describe("sessionRegistry — per-dimension project counters", () => {
  beforeEach(async () => {
    await resetRegistry();
  });

  const NEEDS_DECISION = "NEEDS_USER_DECISION";

  async function seedOne() {
    await bootstrapWith([
      { id: "s1", cwd: "/p", node_id: "primary", is_running: false, unread_count: 0 },
    ]);
    return () => sessionRegistry.getProject("/p", "primary");
  }

  it("running_count counts active and background work, not idle or approval-blocked", async () => {
    await bootstrapWith([
      { id: "act", cwd: "/p", node_id: "primary" },
      { id: "bg", cwd: "/p", node_id: "primary" },
      { id: "idle", cwd: "/p", node_id: "primary" },
      { id: "blocked", cwd: "/p", node_id: "primary" },
    ]);
    for (const [session_id, monitoring_state] of [
      ["act", "active"],
      ["bg", "waiting_on_background"],
      ["idle", "idle"],
      ["blocked", "blocked_on_user"],
    ] as const) {
      eventBus.publish("session_monitoring_changed", {
        session_id,
        monitoring_state,
        cwd: "/p",
        node_id: "primary",
      });
    }
    const agg = sessionRegistry.getProject("/p", "primary");
    // "idle" and "blocked_on_user" are live processes with no work in
    // flight — Idle on the Running dimension.
    expect(agg.running_count).toBe(2);
    // The approval-blocked one shows on its own dimension instead.
    expect(agg.waiting_for_user_count).toBe(1);
  });

  it("errored_count moves on session_error_changed", async () => {
    const agg = await seedOne();
    expect(agg().errored_count).toBe(0);
    eventBus.publish("session_error_changed", { session_id: "s1", has_error: true });
    expect(agg().errored_count).toBe(1);
    eventBus.publish("session_error_changed", { session_id: "s1", has_error: false });
    expect(agg().errored_count).toBe(0);
  });

  it("errored_count clears when a new turn starts on the session", async () => {
    const agg = await seedOne();
    eventBus.publish("session_error_changed", { session_id: "s1", has_error: true });
    expect(agg().errored_count).toBe(1);
    eventBus.publish("turn_start", { app_session_id: "s1" });
    expect(agg().errored_count).toBe(0);
    // …and the same turn_start makes it running.
    expect(agg().running_count).toBe(1);
  });

  it("waiting_for_user_count moves on a pending user-input request", async () => {
    const agg = await seedOne();
    eventBus.publish("session_user_input_changed", {
      session_id: "s1",
      pending_user_input_count: 1,
    });
    expect(agg().waiting_for_user_count).toBe(1);
    eventBus.publish("session_user_input_changed", {
      session_id: "s1",
      pending_user_input_count: 0,
    });
    expect(agg().waiting_for_user_count).toBe(0);
  });

  it("waiting_for_user_count moves on a NEEDS_USER_DECISION marker", async () => {
    const agg = await seedOne();
    eventBus.publish("session_marker_changed", {
      session_id: "s1",
      extension_id: "user-attention",
      marker: { color: "#f80", tooltip: "decide", tag: NEEDS_DECISION },
    });
    expect(agg().waiting_for_user_count).toBe(1);
    eventBus.publish("session_marker_changed", {
      session_id: "s1",
      extension_id: "user-attention",
      marker: null,
    });
    expect(agg().waiting_for_user_count).toBe(0);
  });

  it("a marker that is not NEEDS_USER_DECISION does not mark the session waiting", async () => {
    const agg = await seedOne();
    eventBus.publish("session_marker_changed", {
      session_id: "s1",
      extension_id: "user-attention",
      marker: { color: "#08f", tooltip: "done", tag: "ALL_TASKS__DONE" },
    });
    expect(agg().waiting_for_user_count).toBe(0);
  });

  it("dimensions never mask each other — one session can hit every counter", async () => {
    const agg = await seedOne();
    eventBus.publish("session_monitoring_changed", {
      session_id: "s1",
      monitoring_state: "active",
      cwd: "/p",
      node_id: "primary",
    });
    eventBus.publish("session_unread_changed", {
      session_id: "s1",
      unread_count: 4,
      cwd: "/p",
      node_id: "primary",
    });
    eventBus.publish("session_error_changed", { session_id: "s1", has_error: true });
    eventBus.publish("session_user_input_changed", {
      session_id: "s1",
      pending_user_input_count: 2,
    });
    expect(agg()).toEqual({
      running_count: 1,
      unread_session_count: 1,
      waiting_for_user_count: 1,
      errored_count: 1,
    });
  });

  it("counters are per project — a sibling project is untouched", async () => {
    await bootstrapWith([
      { id: "a", cwd: "/p", node_id: "primary" },
      { id: "b", cwd: "/q", node_id: "primary" },
    ]);
    eventBus.publish("session_error_changed", { session_id: "a", has_error: true });
    expect(sessionRegistry.getProject("/p", "primary").errored_count).toBe(1);
    expect(sessionRegistry.getProject("/q", "primary").errored_count).toBe(0);
  });

  it("a project with only an errored session still has an aggregate entry", async () => {
    const agg = await seedOne();
    eventBus.publish("session_error_changed", { session_id: "s1", has_error: true });
    // Regression: the aggregate map used to be pruned when running and
    // unread were both zero, which would drop a project whose only
    // signal is an error or a waiting-for-user request.
    expect(agg().errored_count).toBe(1);
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

  it("buckets: 6 error, 5 needs-user, 4 new, 3 open-todo, 2 running, 1 done, 0 none", () => {
    expect(statusRankOf({ has_error: true })).toBe(6);
    expect(statusRankOf({ monitoring_state: "active" })).toBe(2);
    expect(statusRankOf({ monitoring_state: "waiting_on_background" })).toBe(2);
    expect(statusRankOf({ monitoring_state: "blocked_on_user" })).toBe(5);
    expect(statusRankOf({ pending_user_input_count: 1 })).toBe(5);
    expect(statusRankOf({ monitoring_state: "idle", markers: m("NEEDS_USER_DECISION") })).toBe(5);
    expect(statusRankOf({ unread_count: 2 })).toBe(4);
    expect(statusRankOf({ monitoring_state: "active", unread_count: 2 })).toBe(2);
    expect(statusRankOf({ current_todos: [{ content: "A", status: "pending" }] })).toBe(3);
    expect(statusRankOf({ current_tasks: [{ content: "A", status: "in_progress" }] })).toBe(3);
    expect(statusRankOf({ markers: m("ALL_TASKS__DONE") })).toBe(1);
    expect(statusRankOf({ monitoring_state: "idle" })).toBe(0);
    expect(statusRankOf({ monitoring_state: "active", markers: m("NEEDS_USER_DECISION") })).toBe(5);
    expect(statusRankOf({ monitoring_state: "active", current_todos: [{ content: "A", status: "pending" }] })).toBe(3);
    expect(statusRankOf({ monitoring_state: "active", unread_count: 2, current_tasks: [{ content: "A", status: "pending" }] })).toBe(3);
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
    expect(statusRankForRow({ id: sid, monitoring_state: "stopped" })).toBe(2);

    eventBus.publish("session_metadata_updated", {
      session_id: sid,
      patch: { current_todos: [{ content: "A", status: "pending" }] },
    });
    expect(statusRankForRow({ id: sid, monitoring_state: "stopped" })).toBe(3);
  });

  it("statusRankForRow falls back to row fields when the sid is unseeded", async () => {
    await resetRegistry();
    expect(statusRankForRow({ id: "deep-page", monitoring_state: "active" })).toBe(2);
    expect(statusRankForRow({ id: "deep-page-input", pending_user_input_count: 1 })).toBe(5);
    expect(statusRankForRow({ id: "deep-page-2", unread_count: 3 })).toBe(4);
    expect(statusRankForRow({ id: "deep-page-4", current_tasks: [{ content: "A", status: "pending" }] })).toBe(3);
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

describe("sessionRegistry — project subscribers", () => {
  beforeEach(async () => {
    await resetRegistry();
  });

  it("subscribeProject fires only for its own project key, then cleans up", async () => {
    await bootstrapWith([
      { id: "pa", cwd: "/p", node_id: "primary", is_running: false, unread_count: 0 },
      { id: "pb", cwd: "/q", node_id: "primary", is_running: false, unread_count: 0 },
    ]);
    let pFires = 0;
    let qFires = 0;
    const offP = sessionRegistry.subscribeProject("/p", "primary", () => pFires++);
    // A second subscriber on the same key reuses the existing listener set.
    let p2Fires = 0;
    const offP2 = sessionRegistry.subscribeProject("/p", "primary", () => p2Fires++);
    const offQ = sessionRegistry.subscribeProject("/q", "primary", () => qFires++);

    eventBus.publish("session_unread_changed", {
      session_id: "pa",
      unread_count: 1,
      cwd: "/p",
      node_id: "primary",
    });
    expect(pFires).toBe(1);
    expect(p2Fires).toBe(1);
    expect(qFires).toBe(0);

    eventBus.publish("session_unread_changed", {
      session_id: "pb",
      unread_count: 1,
      cwd: "/q",
      node_id: "primary",
    });
    expect(pFires).toBe(1);
    expect(qFires).toBe(1);

    // Unsubscribing one of two keeps the set alive for the other. The
    // delta must actually change the aggregate (unread 1→0 flips
    // unread_session_count) for the remaining listener to fire.
    offP();
    eventBus.publish("session_unread_changed", {
      session_id: "pa",
      unread_count: 0,
      cwd: "/p",
      node_id: "primary",
    });
    expect(pFires).toBe(1);
    expect(p2Fires).toBe(2);

    // Last subscriber off → the listener set is pruned from the map.
    offP2();
    offQ();
    p2Fires = 0;
    qFires = 0;
    eventBus.publish("session_unread_changed", {
      session_id: "pa",
      unread_count: 4,
      cwd: "/p",
      node_id: "primary",
    });
    expect(p2Fires).toBe(0);
    expect(qFires).toBe(0);
  });

  it("subscribeProject keys by node id (primary vs secondary are independent)", () => {
    let primaryFires = 0;
    const off = sessionRegistry.subscribeProject("/p", "secondary", () => primaryFires++);
    eventBus.publish("session_unread_changed", {
      session_id: "pn",
      unread_count: 1,
      cwd: "/p",
      node_id: "primary",
    });
    expect(primaryFires).toBe(0);
    off();
  });

  it("bootstrap fans out to every project + session listener via notifyAll", async () => {
    let projectFires = 0;
    let sessionFires = 0;
    const offP = sessionRegistry.subscribeProject("/p", "primary", () => projectFires++);
    const offS = sessionRegistry.subscribeSession("boot-1", () => sessionFires++);
    await bootstrapWith([{ id: "boot-1", cwd: "/p", node_id: "primary", is_running: true }]);
    expect(projectFires).toBeGreaterThanOrEqual(1);
    expect(sessionFires).toBeGreaterThanOrEqual(1);
    offP();
    offS();
  });
});

describe("sessionRegistry — React hooks", () => {
  beforeEach(async () => {
    await resetRegistry();
  });

  it("useSessionMeta returns EMPTY_SESSION for a null sid and live meta for a real one", async () => {
    await bootstrapWith([
      { id: "hook-s", cwd: "/p", node_id: "primary", is_running: false, unread_count: 0 },
    ]);

    const { result: empty, unmount: unmountEmpty } = renderHook(() => useSessionMeta(null));
    expect(empty.current.unread_count).toBe(0);
    expect(empty.current.is_running).toBe(false);
    unmountEmpty();

    const { result, rerender } = renderHook(() => useSessionMeta("hook-s"));
    expect(result.current.unread_count).toBe(0);

    // A bus delta mutates the slice; useSyncExternalStore re-renders.
    act(() => {
      eventBus.publish("session_unread_changed", {
        session_id: "hook-s",
        unread_count: 5,
        cwd: "/p",
        node_id: "primary",
      });
    });
    expect(result.current.unread_count).toBe(5);
    rerender();
    expect(result.current.unread_count).toBe(5);
  });

  it("useProjectAggregate returns EMPTY_AGGREGATE for a null path and live counts for a real one", async () => {
    await bootstrapWith([
      { id: "agg-s", cwd: "/p", node_id: "primary", is_running: true, unread_count: 0 },
    ]);

    const { result: empty, unmount: unmountEmpty } = renderHook(() =>
      useProjectAggregate(null),
    );
    expect(empty.current).toEqual({
      running_count: 0,
      unread_session_count: 0,
      waiting_for_user_count: 0,
      errored_count: 0,
    });
    unmountEmpty();

    const { result } = renderHook(() => useProjectAggregate("/p"));
    expect(result.current.running_count).toBe(1);

    act(() => {
      eventBus.publish("session_unread_changed", {
        session_id: "agg-s",
        unread_count: 2,
        cwd: "/p",
        node_id: "primary",
      });
    });
    expect(result.current.unread_session_count).toBe(1);
  });
});

describe("sessionRegistry — imperative seen/unread acks", () => {
  beforeEach(async () => {
    await resetRegistry();
  });

  it("ackSessionSeen POSTs /seen with the uid and resolves on success", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true });
    (globalThis as unknown as { fetch: typeof fetch }).fetch =
      fetchMock as unknown as typeof fetch;

    await ackSessionSeen("sess ack/1", "user-7");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`/api/sessions/${encodeURIComponent("sess ack/1")}/seen`);
    expect(init).toMatchObject({
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ uid: "user-7" });
  });

  it("ackSessionSeen resolves silently when the POST rejects (no retry)", async () => {
    (globalThis as unknown as { fetch: typeof fetch }).fetch = vi
      .fn()
      .mockRejectedValue(new Error("network down")) as unknown as typeof fetch;

    await expect(ackSessionSeen("sess-fail", null)).resolves.toBeUndefined();
  });

  it("markSessionUnread POSTs /unread and resolves on success", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true });
    (globalThis as unknown as { fetch: typeof fetch }).fetch =
      fetchMock as unknown as typeof fetch;

    await markSessionUnread("sess ack/2");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`/api/sessions/${encodeURIComponent("sess ack/2")}/unread`);
    expect(init).toMatchObject({ method: "POST" });
  });

  it("markSessionUnread resolves silently when the POST rejects", async () => {
    (globalThis as unknown as { fetch: typeof fetch }).fetch = vi
      .fn()
      .mockRejectedValue(new Error("network down")) as unknown as typeof fetch;

    await expect(markSessionUnread("sess-fail")).resolves.toBeUndefined();
  });
});
