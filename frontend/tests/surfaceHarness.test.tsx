// Regression coverage for the native-surface renderApp() harness
// extension (tests/harness/mockBackend.ts's SurfaceSessionState +
// seedSurface/applySurfaceFrame, tests/harness/index.ts's
// h.seedSurface/h.emitSurface, tests/harness/mockWebSocket.ts's
// getCurrentByUrl/emitTo/deliverRaw). This is the foundation Phase I
// stage 2b's chat-content test migration builds on — every migrated
// suite should follow this exact shape: seedSurface() before
// selectSession(), then emitSurface() for live frames instead of the
// legacy h.emit()/h.emitMany().
//
// GOTCHA (post-render seedSurface() vs configureBackend): the post-
// renderApp() `h.seedSurface()` call below only reliably backs the LIVE
// h.emitSurface() path exercised further down, NOT this file's own
// REST-seeded turn ("t1"/"hello") — that content is never independently
// asserted on. With a single session, autoSelectSession fires during
// renderApp()'s own initial mount/flush and can win the race against a
// seedSurface() called after renderApp() resolves, so ChatSurfaceView's
// FIRST fetchSnapshot() can hydrate from the always-empty default
// envelope before the seed lands. A suite that needs its REST-seeded
// (non-live) turns visible on the session's first render must seed via
// `configureBackend` instead (runs before the app mounts, so it always
// wins the race) — see tests/model-switch-grouping.test.tsx for the
// pattern.

import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import { node, promptNode, turnNode, compactTurn, runWire } from "./surface/fixtures";

describe("native surface harness (seedSurface + emitSurface)", () => {
  it("hydrates a seeded turn via ChatSurfaceView, then a live frame updates it", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "s1", messages: [] });
    const h = await renderApp({ seed: { sessions: [session] } });

    const turnId = "t1";
    h.seedSurface("s1", {
      turns: [compactTurn(turnNode(turnId), promptNode(turnId, "hello"), [])],
      runs: [runWire("run-1")],
    });

    await h.selectSession("s1");
    await h.waitFor(() => h.$('[data-testid="surface-chat-view"]') !== null);

    const assistantNode = node({
      node_id: "a1",
      turn_id: turnId,
      kind: "assistant_text",
      status: "complete",
      payload: { text: "PONG" },
    });
    h.emitSurface("s1", {
      cv: 1,
      surface_id: "s1",
      snapshot: { incarnation: "mock", render_rev: 1, hist_rev: 0 },
      node: assistantNode,
      type: "node_upsert",
    });

    await h.waitFor(() => {
      const el = h.$('[data-testid="surface-assistant-text"]');
      return !!el && (el.textContent ?? "").includes("PONG");
    }, 5000);
    expect(h.$('[data-testid="surface-assistant-text"]')?.textContent).toContain("PONG");

    h.unmount();
  });
});
