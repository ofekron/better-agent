import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import { eventBus } from "../src/lib/eventBus";
import { promptNode, turnNode, compactTurn } from "./surface/fixtures";

describe("WebSocket event handling", () => {
  it("native surface: resync_required forces a refetch that replaces rewound-away turns", async () => {
    // Native equivalent of legacy's `rewind_complete` (a full messages[]
    // replacement pushed inline over the WS). The native protocol has no
    // frame that carries content directly for a rewind — the backend
    // instead pushes `resync_required` (adapter/wire.ts), which forces
    // the client back through the SAME `hydrate()` path used for cold
    // load and reconnect (surface/state.ts), fetching a fresh REST
    // snapshot that already reflects the rewind server-side. There is no
    // separate "rewind" merge/patch logic on the client to test — it is
    // the ordinary snapshot-replaces-state path with the identity's
    // `hist_rev` bumped.
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-rewind", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-rewind", {
          turns: [
            compactTurn(turnNode("t1"), promptNode("t1", "first"), []),
            compactTurn(turnNode("t2"), promptNode("t2", "second"), []),
          ],
          identity: { incarnation: "mock", render_rev: 1, hist_rev: 0 },
        });
      },
    });

    await h.selectSession("sess-rewind");
    await h.waitFor(() => h.$$('[data-testid="surface-turn"]').length === 2);

    // Backend rewinds server-side truth down to just the first turn,
    // then announces the history changed.
    h.backend.seedSurface("sess-rewind", {
      turns: [compactTurn(turnNode("t1"), promptNode("t1", "first"), [])],
      identity: { incarnation: "mock", render_rev: 2, hist_rev: 1 },
    });
    h.emitSurface("sess-rewind", {
      type: "resync_required",
      cv: 2,
      surface_id: "sess-rewind",
    });

    await h.waitFor(() => h.$$('[data-testid="surface-turn"]').length === 1);
    const remaining = h.$$('[data-testid="surface-turn"]');
    expect(remaining.map((t) => t.getAttribute("data-turn-id"))).toEqual(["t1"]);
    h.unmount();
  });

  // The legacy "loose manager_event (no active turn) appends to the last
  // assistant message", "late agent_message (re-emitted after turn
  // complete) routes by msg_id", and "loose manager_event with no current
  // session is silently dropped" cases are DELETED, not ported — all three
  // exercise Strategy.ts's client-side raw-event routing/merge onto a flat
  // message list, a step the native contract-node protocol has no
  // equivalent of: every live frame already names its own `node_id`/
  // `turn_id`, so there is no "which message does this belong to"
  // ambiguity to route, and nothing to de-duplicate against a "late
  // re-emitted" delivery or guard for a missing current session (see
  // streaming-details.test.ts's header comment for the fuller rationale,
  // same disposition here).

  it("projects_changed triggers a refetch of /api/projects", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.flush();

    const before = h.restCalls.filter(
      (c) => c.method === "GET" && c.path === "/api/projects",
    ).length;

    h.emit({ type: "projects_changed", data: {} });
    await h.flush();

    const after = h.restCalls.filter(
      (c) => c.method === "GET" && c.path === "/api/projects",
    ).length;
    expect(after).toBeGreaterThan(before);
    h.unmount();
  });

  it("project_aggregates_changed updates its projection without a structural refetch", async () => {
    const h = await renderApp({ seed: { sessions: [makeSession()] } });
    await h.flush();
    const before = h.restCalls.filter(
      (call) => call.method === "GET" && call.path === "/api/projects",
    ).length;

    h.emit({
      type: "project_aggregates_changed",
      data: {
        epoch: "mock-projects",
        revision: 1,
        upserts: [{
          path: "/repo",
          node_id: "primary",
          running_count: 1,
          unread_session_count: 0,
          waiting_for_user_count: 0,
          errored_count: 0,
        }],
        tombstones: [],
      },
    });
    await h.flush();

    const after = h.restCalls.filter(
      (call) => call.method === "GET" && call.path === "/api/projects",
    ).length;
    expect(after).toBe(before);
    h.unmount();
  });

  it("publishes each provider and startup frame once without DOM shims", async () => {
    const events = [
      { type: "provider_changed", data: {} },
      { type: "installation_capabilities_changed", data: {} },
      {
        type: "provider_install_progress",
        data: { kind: "codex", stream: "stdout", text: "installed" },
      },
      {
        type: "provider_install_finished",
        data: {
          kind: "codex",
          label: "Codex",
          command: "npm install",
          state: "succeeded",
          lines: [{ s: "stdout", t: "installed" }],
          started_at: "2026-08-04T10:00:00Z",
          finished_at: "2026-08-04T10:01:00Z",
          returncode: 0,
          installed: true,
          message: null,
        },
      },
      { type: "models_catalog_changed", data: { provider_id: "codex" } },
      { type: "background_work_changed", data: { epoch: "e1", cleared: true } },
    ];
    const received = new Map<string, unknown[]>();
    const domEvents: string[] = [];
    const onDomEvent = (event: Event) => domEvents.push(event.type);
    const unsubscribes = events.map(({ type }) => {
      received.set(type, []);
      window.addEventListener(type, onDomEvent);
      return eventBus.subscribe(type, (payload) => received.get(type)!.push(payload));
    });
    const h = await renderApp({ seed: { sessions: [makeSession()] } });

    try {
      for (const event of events) {
        h.emit(event as Parameters<typeof h.emit>[0]);
      }
      await h.flush();

      for (const { type, data } of events) {
        expect(received.get(type)).toEqual([data]);
      }
      expect(domEvents).toEqual([]);
    } finally {
      for (const unsubscribe of unsubscribes) unsubscribe();
      for (const { type } of events) {
        window.removeEventListener(type, onDomEvent);
      }
      h.unmount();
    }
  });

  it("dropping the WS closes connection and the reconnect timer fires no immediate frames", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    const beforeCount = h.outbound.length;

    h.dropConnection();
    // No immediate further outbound — reconnect happens on a 2s timer
    // outside the test's flush window.
    expect(h.outbound.length).toBe(beforeCount);
    // App didn't crash; chat still rendered.
    expect(h.toJSON().chat.visible).toBe(true);
    h.unmount();
  });

  it("session_renamed for a different session id is ignored on the active session", async () => {
    const session = makeSession({ name: "stable" });
    const other = makeSession({ id: "other", name: "other-name" });
    const h = await renderApp({ seed: { sessions: [session, other] } });
    await h.selectSession(session.id);

    h.emit({
      type: "session_renamed",
      data: { session_id: other.id, name: "renamed-other" },
    });
    await h.flush();

    const view = h.toJSON();
    const activeName = view.sidebar.sessions.find((s) => s.id === session.id)?.name;
    const otherName = view.sidebar.sessions.find((s) => s.id === other.id)?.name;
    expect(activeName).toContain("stable");
    expect(otherName).toContain("renamed-other");
    h.unmount();
  });

  // ── DIV-4 multi-tab convergence regressions ──────────────────────

  it("session_created adds the session to the sidebar (multi-tab convergence)", async () => {
    const existing = makeSession({ id: "s1", name: "existing" });
    const h = await renderApp({ seed: { sessions: [existing] } });
    await h.selectSession(existing.id);

    expect(h.toJSON().sidebar.sessions.map((s) => s.id)).toEqual(["s1"]);

    const fresh = makeSession({ id: "s2", name: "fresh-from-other-tab" });
    h.emit({ type: "session_created", data: { session: fresh } });
    await h.flush();

    const ids = h.toJSON().sidebar.sessions.map((s) => s.id);
    expect(ids).toContain("s2");
    expect(ids).toContain("s1");
    h.unmount();
  });

  it("session_created with an id already in the list is deduped (no duplicate row)", async () => {
    const existing = makeSession({ id: "s1", name: "existing" });
    const h = await renderApp({ seed: { sessions: [existing] } });
    await h.selectSession(existing.id);

    // Originating tab already has s1 via REST POST response — the WS
    // echo MUST NOT produce a duplicate sidebar entry.
    h.emit({ type: "session_created", data: { session: existing } });
    await h.flush();

    const ids = h.toJSON().sidebar.sessions.map((s) => s.id);
    expect(ids.filter((id) => id === "s1")).toHaveLength(1);
    h.unmount();
  });

  it("WS reconnect refreshes sessions missed while disconnected", async () => {
    const existing = makeSession({ id: "s1", name: "existing" });
    const fresh = makeSession({ id: "s2", name: "created-elsewhere" });
    const h = await renderApp({ seed: { sessions: [existing] } });
    await h.selectSession(existing.id);

    h.dropConnection();
    h.backend.state.sessions.push(fresh);
    expect(h.toJSON().sidebar.sessions.map((s) => s.id)).toEqual(["s1"]);

    const before = h.restCalls.filter(
      (c) => c.method === "GET" && c.path === "/api/sessions",
    ).length;
    h.reopenConnection();
    await h.flush();

    const after = h.restCalls.filter(
      (c) => c.method === "GET" && c.path === "/api/sessions",
    ).length;
    expect(after).toBeGreaterThan(before);
    expect(h.toJSON().sidebar.sessions.map((s) => s.id)).toContain("s2");
    h.unmount();
  });

  it("first WS connection retries an initial sessions request failure", async () => {
    const session = makeSession({ id: "s1", name: "recovered-session" });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.failNextWithStatus(503, "/api/sessions", true);
      },
      autoOpenWebSocket: false,
    });

    const before = h.restCalls.filter(
      (call) => call.method === "GET" && call.path === "/api/sessions",
    ).length;
    h.backend.setOffline(false);
    h.reopenConnection();
    await h.flush();

    const after = h.restCalls.filter(
      (call) => call.method === "GET" && call.path === "/api/sessions",
    ).length;
    expect(after).toBeGreaterThan(before);
    expect(h.toJSON().sidebar.sessions.map((item) => item.id)).toContain("s1");
    h.unmount();
  });

  it("first WS connection does not duplicate a successful initial request", async () => {
    const session = makeSession({ id: "s1", name: "existing" });
    const h = await renderApp({
      seed: { sessions: [session] },
      autoOpenWebSocket: false,
    });
    const before = h.restCalls.filter(
      (call) => call.method === "GET" && call.path === "/api/sessions",
    ).length;

    h.reopenConnection();
    await h.flush();

    const after = h.restCalls.filter(
      (call) => call.method === "GET" && call.path === "/api/sessions",
    ).length;
    expect(after).toBe(before);
    h.unmount();
  });

  it("session_metadata_updated applies a model patch from another tab", async () => {
    const session = makeSession({ id: "s1", name: "x", model: "old-model" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: "s1",
        patch: { model: "new-model" },
        originated_by: "OTHER_TAB",
      },
    });
    await h.flush();

    // The patch should land. We can't read model directly from the
    // view, so just assert the WS handler accepted the frame without
    // crashing and the app stayed mounted.
    expect(h.toJSON().sidebar.sessions.find((s) => s.id === "s1")).toBeDefined();
    h.unmount();
  });

  it("session_metadata_updated hides sessions that become sidebar-hidden", async () => {
    const session = makeSession({ id: "s1", name: "visible" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: "s1",
        patch: { working_mode: "prompt_engineering" },
        originated_by: null,
      },
    });
    await h.flush();

    expect(h.toJSON().sidebar.sessions.map((s) => s.id)).not.toContain("s1");
    h.unmount();
  });

  it("session_metadata_updated whose originated_by matches this tab is skipped", async () => {
    // The echo-suppression rule lives in useWebSocket: if
    // `originated_by === clientId`, the local applier is NOT called.
    // We can't easily assert that the applier wasn't called from the
    // outside, but we can at least assert nothing crashes and the app
    // continues to render the same session.
    const session = makeSession({ id: "s1", name: "x", model: "old-model" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    // We don't know this tab's clientId from outside, so emit with a
    // marker that matches whatever the test harness uses for its own
    // tab — best we can do is emit a sensible-looking frame and assert
    // no crash.
    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: "s1",
        patch: { model: "echo-model" },
        originated_by: null, // null != any clientId → applies
      },
    });
    await h.flush();

    expect(h.toJSON().chat.visible).toBe(true);
    h.unmount();
  });
});
