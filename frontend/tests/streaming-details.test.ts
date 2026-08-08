import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeRun, makeSession } from "./fixtures";

// The four legacy client-side live-event-merge tests formerly here
// (routing a live event to the active run's target message instead of
// blindly the last one; manager_event text accumulating onto a canonical
// assistant bubble; worker_start adding a panel to that bubble; turn_start
// re-broadcasts not wiping accumulated content) are DELETED, not ported —
// see the migration ledger. Every one of them guards a hazard specific to
// Strategy.ts's client-side flat-message-list event merge (ambiguous
// "which message is this live event for", or "does a re-broadcast wipe
// what I already merged"). The native contract-node protocol has no
// client-side merge step to guard: every live frame (`node_upsert`,
// `text_delta`, `node_status`) is explicitly addressed by `node_id`/
// `turn_id` from the backend, and content already applied to a node's
// table is never replaced by a later structurally-unrelated frame — so
// none of these hazards can arise by construction. Native live-frame
// accumulation itself is already covered by tests/surfaceHarness.test.tsx
// and tests/surfaceStore-live-turn-birth.test.tsx; worker/subagent
// structured rendering by tests/surface/toolInteraction.test.tsx.

describe("live event accumulation onto canonical messages", () => {
  it("workers_changed event with cwd=null (global) refetches even when cwd differs", async () => {
    const session = makeSession({ cwd: "/proj/a", orchestration_mode: "team" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.flush();

    const before = h.restCalls.filter(
      (c) => c.method === "GET" && c.path === "/api/workers",
    ).length;

    h.emit({ type: "workers_changed", data: { cwd: null } });
    await h.flush();

    const after = h.restCalls.filter(
      (c) => c.method === "GET" && c.path === "/api/workers",
    ).length;
    expect(after).toBeGreaterThan(before);
    h.unmount();
  });

  it("workers_changed for a different cwd refetches global workers", async () => {
    const session = makeSession({ cwd: "/proj/a", orchestration_mode: "team" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.flush();

    const before = h.restCalls.filter(
      (c) => c.method === "GET" && c.path === "/api/workers",
    ).length;

    h.emit({ type: "workers_changed", data: { cwd: "/proj/b" } });
    await h.flush();

    const after = h.restCalls.filter(
      (c) => c.method === "GET" && c.path === "/api/workers",
    ).length;
    expect(after).toBeGreaterThan(before);
    h.unmount();
  });

  it("a second send while a run is active does not crash and queues the WS frame", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.typeAndSend("first");

    h.emit({
      type: "run_state",
      data: {
        app_session_id: session.id,
        runs: [makeRun({ target_message_id: null })],
      },
    });
    await h.flush();

    await h.typeAndSend("second");

    const sends = h.outbound.filter((f) => f.type === "send_message");
    expect(sends).toHaveLength(2);
    expect(sends[0]).toMatchObject({ prompt: "first" });
    expect(sends[1]).toMatchObject({ prompt: "second" });
    h.unmount();
  });
});
