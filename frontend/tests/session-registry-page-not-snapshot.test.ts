import { describe, it, expect, beforeEach, vi } from "vitest";

import { eventBus } from "../src/lib/eventBus";
import { sessionRegistry } from "../src/lib/sessionRegistry";

/**
 * Regression guard: `/api/sessions` is a PAGE, never a snapshot.
 *
 * The endpoint is paginated (offset/limit, default 50) and filtered
 * (archived excluded; foldered/pinned/empty sessions outrank recency), so
 * the open session is routinely absent from the page the registry
 * bootstraps and refreshes from. Wiping the map from that page reported
 * the open, RUNNING session as `is_running: false`, which in turn made
 * `TurnGroup`'s `defaultCollapsed = hasResponse && !isRunning` flip true
 * and collapsed the user's assistant response with no click — every tab
 * focus (`visibilitychange`/`focus` → `bootstrap()`) and every sidebar
 * refresh.
 *
 * Two invariants are locked here:
 *  1. A page never evicts sids it does not contain. Only `session_deleted`
 *     removes an entry.
 *  2. A page never regresses an entry a WS delta moved after the request
 *     was dispatched (`beginPageFetch()` watermark).
 */

type SessionRow = {
  id: string;
  cwd?: string;
  node_id?: string;
  is_running?: boolean;
  monitoring_state?: string;
  unread_count?: number;
};

function stubSessionsResponse(sessions: SessionRow[], onFetch?: () => void) {
  const fetchMock = vi.fn().mockImplementation(async () => {
    onFetch?.();
    return { ok: true, json: async () => ({ sessions }) };
  });
  (globalThis as unknown as { fetch: typeof fetch }).fetch =
    fetchMock as unknown as typeof fetch;
  return fetchMock;
}

function reset() {
  (sessionRegistry as unknown as { __resetForTests: () => void }).__resetForTests();
  sessionRegistry.bind();
}

describe("sessionRegistry — a listing page is not a complete snapshot", () => {
  beforeEach(() => {
    reset();
  });

  it("keeps a running session that the bootstrap page omits", async () => {
    const sid = "open-session-below-page-1";
    stubSessionsResponse([]);
    await sessionRegistry.bootstrap();

    eventBus.publish("session_created", {
      session: { id: sid, cwd: "/repo", node_id: "primary" },
    });
    eventBus.publish("session_monitoring_changed", {
      session_id: sid,
      monitoring_state: "active",
      cwd: "/repo",
      node_id: "primary",
    });
    expect(sessionRegistry.getSession(sid).is_running).toBe(true);

    // Tab focus: bootstrap again. The page holds only the top-ranked
    // sessions, not this one.
    stubSessionsResponse([
      { id: "other", cwd: "/repo", node_id: "primary", is_running: false },
    ]);
    await sessionRegistry.bootstrap();

    expect(sessionRegistry.getSession(sid).is_running).toBe(true);
    expect(sessionRegistry.getSession(sid).monitoring_state).toBe("active");
  });

  it("does not let an in-flight page regress a newer WS delta", async () => {
    const sid = "races-with-page";
    stubSessionsResponse([]);
    await sessionRegistry.bootstrap();

    eventBus.publish("session_created", {
      session: { id: sid, cwd: "/repo", node_id: "primary" },
    });
    expect(sessionRegistry.getSession(sid).is_running).toBe(false);

    // The page was built while the session was idle; the turn starts
    // while the request is in flight.
    stubSessionsResponse(
      [{ id: sid, cwd: "/repo", node_id: "primary", monitoring_state: "stopped" }],
      () => {
        eventBus.publish("session_monitoring_changed", {
          session_id: sid,
          monitoring_state: "active",
          cwd: "/repo",
          node_id: "primary",
        });
      },
    );
    await sessionRegistry.bootstrap();

    expect(sessionRegistry.getSession(sid).is_running).toBe(true);
  });

  it("still applies page rows for sessions no delta touched", async () => {
    stubSessionsResponse([]);
    await sessionRegistry.bootstrap();

    stubSessionsResponse([
      { id: "fresh", cwd: "/repo", node_id: "primary", is_running: true, unread_count: 3 },
    ]);
    await sessionRegistry.bootstrap();

    expect(sessionRegistry.getSession("fresh").is_running).toBe(true);
    expect(sessionRegistry.getSession("fresh").unread_count).toBe(3);
  });

  it("does not let a stale page regress a page that already landed", async () => {
    const sid = "two-pages-in-flight";
    stubSessionsResponse([]);
    await sessionRegistry.bootstrap();
    eventBus.publish("session_created", {
      session: { id: sid, cwd: "/repo", node_id: "primary" },
    });

    // Page A dispatched first (session idle at snapshot time).
    const seqA = sessionRegistry.beginPageFetch();
    // Page B dispatched later and lands first, carrying the running state.
    const seqB = sessionRegistry.beginPageFetch();
    sessionRegistry.mergeFromRows(
      [{ id: sid, cwd: "/repo", node_id: "primary", is_running: true }],
      seqB,
    );
    expect(sessionRegistry.getSession(sid).is_running).toBe(true);

    // Page A resolves afterwards — it must not win.
    sessionRegistry.mergeFromRows(
      [{ id: sid, cwd: "/repo", node_id: "primary", is_running: false }],
      seqA,
    );
    expect(sessionRegistry.getSession(sid).is_running).toBe(true);
  });

  it("evicts only on session_deleted", async () => {
    const sid = "deleted-while-page-in-flight";
    stubSessionsResponse([
      { id: sid, cwd: "/repo", node_id: "primary", is_running: true },
    ]);
    await sessionRegistry.bootstrap();
    expect(sessionRegistry.getSession(sid).is_running).toBe(true);

    // A page dispatched before the delete must not resurrect it.
    stubSessionsResponse(
      [{ id: sid, cwd: "/repo", node_id: "primary", is_running: true }],
      () => {
        eventBus.publish("session_deleted", { session_id: sid });
      },
    );
    await sessionRegistry.bootstrap();

    expect(sessionRegistry.getSession(sid).is_running).toBe(false);
    expect(sessionRegistry.getSession(sid).monitoring_state).toBe("stopped");
  });
});
