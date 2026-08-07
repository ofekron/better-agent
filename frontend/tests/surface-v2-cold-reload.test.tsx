/**
 * Regression repro for the "reload never renders the assistant message"
 * defect observed in live validation (session 79fc3ce3-aed9-4474-97c8-
 * 052fa1a511ac): after a completed v2 turn, a fresh mount + selectSession
 * (exactly what a page reload does — App.tsx's route effect calls
 * selectSession(route.sessionId) once `sessionsLoaded`) must end up with
 * the assistant's content in `currentSession.messages`, sourced entirely
 * from useSurfaceSession's cold hydrate() -> onSnapshot (replaceMessages)
 * path — no live turn, no WS content frames involved.
 *
 * The turn/prompt/result/live_turn_nodes payload below is copied verbatim
 * from the live-validation evidence's snapshot.json (GET
 * /api/v2/surface/sessions/{id}/snapshot response body), not a synthetic
 * fixture — this is the exact shape the backend served in the failing run.
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useSession } from "../src/hooks/useSession";
import type { Session } from "../src/types";
import { MockWebSocketController } from "./harness/mockWebSocket";

const SESSION_ID = "79fc3ce3-aed9-4474-97c8-052fa1a511ac";
const SESSION_FETCH = /\/api\/sessions\/[^?]+\?.*exchange_count=/;

// Verbatim from the evidence's snapshot.json body.turns[0] / live_turn_nodes.
const TURN_NODE = {
  cv: 1, node_id: "turn:a444260e-7f81-4236-9dbf-639e239c455f", parent_id: SESSION_ID,
  turn_id: "a444260e-7f81-4236-9dbf-639e239c455f", surface_id: SESSION_ID, kind: "turn",
  ts: 1786138529.181079, seq: 9, status: null, payload: null, run_ref: null,
  sidecar_ref: null, target_ref: null,
  child_manifest: { renderable_child_count: 1, has_children: true },
};
const PROMPT_NODE = {
  cv: 1, node_id: "a444260e-7f81-4236-9dbf-639e239c455f", parent_id: "turn:a444260e-7f81-4236-9dbf-639e239c455f",
  turn_id: "a444260e-7f81-4236-9dbf-639e239c455f", surface_id: SESSION_ID, kind: "typed_prompt",
  ts: 1786138529.181079, seq: 9, status: "complete",
  payload: {
    text: "Reply with exactly the single word: PONG. No punctuation, no other words.",
    attachments: [], send_mode: "queue", origin: "user", source_session_ref: null,
    sent_text: null, intent_id: null,
  },
  run_ref: null, sidecar_ref: null, target_ref: null, child_manifest: null,
};
const ASSISTANT_TEXT_NODE = {
  cv: 1, node_id: "d5255ea9-9740-43d0-b500-ec60fee6c7ca", parent_id: "turn:a444260e-7f81-4236-9dbf-639e239c455f",
  turn_id: "a444260e-7f81-4236-9dbf-639e239c455f", surface_id: SESSION_ID, kind: "assistant_text",
  ts: 1786138541.769324, seq: 28, status: "complete", payload: { text: "PONG" },
  run_ref: null, sidecar_ref: null, target_ref: null, child_manifest: null,
};
const EXPLANATION_NODE = {
  cv: 1, node_id: "explanation:051b2aa8-8447-429b-a3b2-b84227ef6d3d", parent_id: "turn:a444260e-7f81-4236-9dbf-639e239c455f",
  turn_id: "a444260e-7f81-4236-9dbf-639e239c455f", surface_id: SESSION_ID, kind: "explanation",
  ts: 1786138541.766276, seq: 27, status: null, payload: null, run_ref: null,
  sidecar_ref: null, target_ref: null,
  child_manifest: { renderable_child_count: 1, has_children: true },
};
const THINKING_NODE = {
  cv: 1, node_id: "051b2aa8-8447-429b-a3b2-b84227ef6d3d", parent_id: "explanation:051b2aa8-8447-429b-a3b2-b84227ef6d3d",
  turn_id: "a444260e-7f81-4236-9dbf-639e239c455f", surface_id: SESSION_ID, kind: "thinking",
  ts: 1786138541.766276, seq: 27, status: "complete",
  payload: {
    text: "The user is asking me to reply with exactly the single word \"PONG\" with no punctuation and no other words. This is a simple, direct request.\n\nLet me follow the instruction precisely.",
    redacted: false,
  },
  run_ref: null, sidecar_ref: null, target_ref: null, child_manifest: null,
};

const REAL_SNAPSHOT_BODY = {
  session_id: SESSION_ID, surface_id: SESSION_ID, instruction_widget: null,
  turns: [
    {
      turn: TURN_NODE, prompt: PROMPT_NODE, results: [ASSISTANT_TEXT_NODE],
      manifest: { renderable_child_count: 1, has_children: true }, runtime_change: null,
    },
  ],
  live_turn_nodes: [TURN_NODE, PROMPT_NODE, ASSISTANT_TEXT_NODE, EXPLANATION_NODE, THINKING_NODE],
  runs: [], older_cursor: null, kind: "ok",
  snapshot_identity: { incarnation: "cb1f8aacd352bfdf", render_rev: 30, hist_rev: 0 },
};

function makeSession(overrides: Partial<Session> = {}): Session {
  const now = new Date().toISOString();
  return {
    id: SESSION_ID, name: "PONG test", model: "claude-haiku-4-5", cwd: "/tmp/proj",
    orchestration_mode: "manager", created_at: now, updated_at: now, messages: [],
    ...overrides,
  };
}

describe("surface v2 cold reload — completed session, fresh mount", () => {
  let ws: MockWebSocketController | null = null;
  afterEach(() => {
    ws?.uninstall();
    ws = null;
    vi.unstubAllGlobals();
  });

  it("hydrates the completed assistant turn into currentSession.messages purely from the v2 snapshot fetch (no WS content, no live turn)", async () => {
    localStorage.setItem("ba.surface_v2", "1");
    ws = new MockWebSocketController();
    ws.install();

    const session = makeSession();
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/v2/surface/")) {
        return Promise.resolve(new Response(JSON.stringify(REAL_SNAPSHOT_BODY), {
          status: 200, headers: { "content-type": "application/json" },
        }));
      }
      if (SESSION_FETCH.test(url)) {
        // Legacy REST tree — v2 kill-the-flash discards this node's
        // messages regardless of what's returned here (see
        // preserveSurfaceOwnedMessages in useSession.ts), so an empty
        // legacy body is the correct "backend serves everything
        // correctly" shape for a v2-owned session.
        return Promise.resolve(new Response(JSON.stringify({ ...session, messages: [] }), {
          status: 200, headers: { "content-type": "application/json" },
        }));
      }
      if (url.includes("/api/sessions?")) {
        return Promise.resolve(new Response(JSON.stringify({ sessions: [session] }), {
          status: 200, headers: { "content-type": "application/json" },
        }));
      }
      return Promise.resolve(new Response(JSON.stringify({}), {
        status: 200, headers: { "content-type": "application/json" },
      }));
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.sessions.map((s) => s.id)).toEqual([SESSION_ID]);
    });

    // Fresh mount + selectSession — exactly what App.tsx's route effect
    // does on a page reload once `sessionsLoaded` is true and the id is
    // already known (from the sidebar fetch above).
    await act(async () => {
      await result.current.selectSession(SESSION_ID);
    });

    // useSurfaceSession's hydrate() effect fires once wsTargetSessionId
    // updates (post REST-select) and calls onSnapshot (replaceMessages)
    // after its own async fetchSnapshot() resolves.
    await waitFor(() => {
      expect(result.current.currentSession?.messages?.some((m) => m.role === "assistant")).toBe(true);
    });

    const assistant = result.current.currentSession?.messages?.find((m) => m.role === "assistant");
    expect(assistant?.content).toBe("PONG");
    const user = result.current.currentSession?.messages?.find((m) => m.role === "user");
    expect(user?.content).toContain("PONG");
  });

  it("ROOT CAUSE: a legacy turn_complete replay on the reload's fresh /ws/chat connection wipes the just-hydrated v2 content and nothing repopulates it", async () => {
    // Live-validation evidence (79fc3ce3-...ws-frames.json, frames 108-126):
    // on RELOAD, the legacy /ws/chat socket resubscribes and the backend
    // replays the completed turn's full legacy backlog to the fresh
    // connection, INCLUDING `turn_complete` frames — even though the turn
    // finished before this connection ever existed. App.tsx's
    // onTurnTerminal handler unconditionally calls
    // `applySessionReconciled(sessionId)` on every such frame. Reconcile's
    // OWN REST fetch is independent of (and asynchronously races)
    // useSurfaceSession's v2 hydrate() fetch — whichever resolves LAST
    // wins. `applySessionReconciled` (useSession.ts) must therefore
    // PRESERVE the v2-owned node's current `messages` on every reconcile
    // apply rather than forcing them to `[]` — useSurfaceSession only
    // re-hydrates on an explicit `resync_required` v2 frame, which this
    // legacy-side reconcile never sends, so nothing would ever repopulate
    // a forced-empty result.
    localStorage.setItem("ba.surface_v2", "1");
    ws = new MockWebSocketController();
    ws.install();

    const session = makeSession();
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      if (url.includes("/api/v2/surface/")) {
        return Promise.resolve(new Response(JSON.stringify(REAL_SNAPSHOT_BODY), {
          status: 200, headers: { "content-type": "application/json" },
        }));
      }
      if (SESSION_FETCH.test(url)) {
        return Promise.resolve(new Response(JSON.stringify({ ...session, messages: [] }), {
          status: 200, headers: { "content-type": "application/json" },
        }));
      }
      if (url.includes("/api/sessions?")) {
        return Promise.resolve(new Response(JSON.stringify({ sessions: [session] }), {
          status: 200, headers: { "content-type": "application/json" },
        }));
      }
      return Promise.resolve(new Response(JSON.stringify({}), {
        status: 200, headers: { "content-type": "application/json" },
      }));
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.sessions.map((s) => s.id)).toEqual([SESSION_ID]);
    });

    await act(async () => {
      await result.current.selectSession(SESSION_ID);
    });
    await waitFor(() => {
      expect(result.current.currentSession?.messages?.some((m) => m.role === "assistant")).toBe(true);
    });

    // The reload's fresh /ws/chat connection replays the completed turn's
    // backlog, including a `turn_complete` frame — App.tsx's
    // onTurnTerminal handler reacts by calling applySessionReconciled.
    await act(async () => {
      await result.current.applySessionReconciled(SESSION_ID);
    });

    // BUG: the assistant message that was already correctly rendered from
    // the v2 snapshot is now gone, and nothing will ever bring it back —
    // useSurfaceSession already hydrated once for this mount and only
    // re-hydrates on a v2 resync_required frame, which never fires here.
    expect(
      result.current.currentSession?.messages?.some((m) => m.role === "assistant"),
    ).toBe(true);
  });
});
