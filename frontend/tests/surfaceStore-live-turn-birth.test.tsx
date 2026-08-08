// Repro for the live-validation finding (session 79fc3ce3-*): a session
// created fresh and streamed live never renders through ChatSurfaceView,
// even though /ws/v2/surface delivers correct node_upsert frames for
// typed_prompt/thinking/assistant_text — see
// .../live-validation/79fc3ce3-*.v2-content-frames.json and
// .../79fc3ce3-*.ws-frames.json (the FIXTURE_FRAMES below are those exact
// payloads, verbatim, including the real backend's parent_id: null
// convention for turn-level body items and the mid-stream re-parent of the
// assistant_text node onto the thinking node's id).
//
// Root cause (frontend/src/surface/state.ts): a fresh session hydrates
// against an EMPTY snapshot (turnOrder: [], childrenTables: {}). Three
// independent drops then combine to swallow every live frame for that
// turn:
//   1. `upsertIntoContainer` bailed out silently whenever the target
//      container's children table wasn't already materialized — true for
//      EVERY container of a turn born entirely from live frames, since
//      only `hydrate()`'s `seedLiveTurnNodes` ever created one.
//   2. The generic node_upsert branch only called `upsertIntoContainer`
//      when `node.parent_id` was non-null — but the real backend sends
//      `parent_id: null` for a node directly under the turn (confirmed by
//      TurnView's own `useChildren(store, entry.turn.node_id, ...)` call,
//      which reads that exact container id).
//   3. `turn_lifecycle`'s `if (!entry) return;` dropped the phase update
//      for a turn this store hadn't birthed yet, and — in the real trace
//      — the ONE turn_lifecycle frame ever sent carries a different
//      (provisional/pre-anchor) turn_id than the one the typed_prompt
//      later establishes, so `entry.phase` never becomes "running" and
//      TurnView's `isLive(turn)` check (state.ts's own LIVE_PHASES) never
//      opts into rendering the live body at all — independent of (1)/(2).

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import type { NodeUpsertFrame, NodeWire, SnapshotEnvelope, TurnLifecycleFrame } from "../src/adapter/wire";

// ---- Fake transport (same shape as surfaceStore-native-empty.test.tsx) --

interface CapturedSocket {
  handlers: {
    onFrame: (frame: NodeUpsertFrame | TurnLifecycleFrame) => void;
    onResyncRequired: () => void;
  };
  open: ReturnType<typeof vi.fn>;
  updateCursors: ReturnType<typeof vi.fn>;
  trackCursor: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  // `lib/runSummaryRegistry.ts` (via `TurnView.tsx`'s `useRunSummaryByTurn`,
  // B1/B3 restoration) opens its own socket through this SAME mocked
  // client — its `createFeedSocketRegistry` calls `.open()`/`.setFeeds()`
  // unconditionally on ensure-open, so the fake must implement the full
  // `SurfaceSocket` public shape, not just what `SurfaceStore` itself uses.
  setFeeds: ReturnType<typeof vi.fn>;
  submit: ReturnType<typeof vi.fn>;
}

let capturedSockets: CapturedSocket[] = [];
let fetchSnapshotImpl: (sessionId: string) => Promise<SnapshotEnvelope> = () =>
  Promise.resolve(EMPTY_SNAPSHOT("s1"));

vi.mock("../src/adapter/client", () => {
  class FakeSurfaceClient {
    fetchSnapshot(sessionId: string) {
      return fetchSnapshotImpl(sessionId);
    }
    fetchChildren() {
      return Promise.resolve({ kind: "ok", value: [], snapshot_identity: IDENTITY });
    }
    fetchOlder() {
      return Promise.resolve({ kind: "ok", turns: [], runs: [], older_cursor: null, snapshot_identity: IDENTITY });
    }
    fetchTurnBody() {
      return Promise.resolve([]);
    }
    // `runSummaryRegistry.ts`'s cold-hydrate path (`useRunSummaryByTurn`,
    // B1/B3 restoration) — an empty run list is a normal, honest "no known
    // run yet" outcome for a fresh session, not an error.
    fetchRuns() {
      return Promise.resolve({ kind: "ok" as const, runs: [], next_cursor: null, snapshot_identity: IDENTITY });
    }
    openSocket(handlers: CapturedSocket["handlers"]) {
      const socket: CapturedSocket = {
        handlers,
        open: vi.fn(),
        updateCursors: vi.fn(),
        trackCursor: vi.fn(),
        close: vi.fn(),
        setFeeds: vi.fn(),
        submit: vi.fn(),
      };
      capturedSockets.push(socket);
      return socket;
    }
  }
  return { SurfaceClient: FakeSurfaceClient };
});

const IDENTITY = { incarnation: "cb1f8aacd352bfdf", render_rev: 0, hist_rev: 0 };

function EMPTY_SNAPSHOT(sessionId: string): SnapshotEnvelope {
  return {
    kind: "ok",
    snapshot_identity: IDENTITY,
    session_id: sessionId,
    surface_id: sessionId,
    instruction_widget: null,
    turns: [],
    live_turn_nodes: [],
    runs: [],
    older_cursor: null,
  };
}

afterEach(() => {
  cleanup();
  capturedSockets = [];
  vi.resetModules();
});

// ---- Literal evidence: .../live-validation/79fc3ce3-*.ws-frames.json ----
// (indices 49/54/92/93/130/132/135 of the recorded /ws/v2/surface `recv`
// stream, reproduced verbatim — see .../79fc3ce3-*.v2-content-frames.json)

const SURFACE_ID = "79fc3ce3-aed9-4474-97c8-052fa1a511ac";
const TURN_ID = "a444260e-7f81-4236-9dbf-639e239c455f";
const THINKING_ID = "051b2aa8-8447-429b-a3b2-b84227ef6d3d";
const ASSISTANT_ID = "d5255ea9-9740-43d0-b500-ec60fee6c7ca";

// The one turn_lifecycle frame observed in the whole trace — tagged with a
// DIFFERENT (provisional) turn_id than the typed_prompt below ever uses.
const LIFECYCLE_FRAME: TurnLifecycleFrame = {
  cv: 1,
  surface_id: SURFACE_ID,
  snapshot: { incarnation: "cb1f8aacd352bfdf", render_rev: 7, hist_rev: 0 },
  turn_id: "a9690776-efed-4aad-b799-543572f05904",
  phase: "running",
  reason: null,
  usage: null,
  type: "turn_lifecycle",
};

const TYPED_PROMPT_NODE: NodeWire = {
  cv: 1,
  node_id: TURN_ID,
  parent_id: null,
  turn_id: TURN_ID,
  surface_id: SURFACE_ID,
  kind: "typed_prompt",
  ts: 1786138529.181079,
  seq: 9,
  status: "complete",
  payload: {
    text: "Reply with exactly the single word: PONG. No punctuation, no other words.",
    attachments: [],
    send_mode: "queue",
    origin: "user",
    source_session_ref: null,
    sent_text: null,
    intent_id: null,
  },
  run_ref: null,
  sidecar_ref: null,
  target_ref: null,
  child_manifest: null,
};

const THINKING_NODE_V1: NodeWire = {
  cv: 1,
  node_id: THINKING_ID,
  parent_id: null,
  turn_id: TURN_ID,
  surface_id: SURFACE_ID,
  kind: "thinking",
  ts: 1786138541.766276,
  seq: 27,
  status: "complete",
  payload: {
    text: 'The user is asking me to reply with exactly the single word "PONG" with no punctuation and no other words. This is a simple, direct request.\n\nLet me follow the instruction precisely.',
    redacted: false,
  },
  run_ref: null,
  sidecar_ref: null,
  target_ref: null,
  child_manifest: null,
};

const ASSISTANT_TEXT_NODE_V1: NodeWire = {
  cv: 1,
  node_id: ASSISTANT_ID,
  parent_id: null,
  turn_id: TURN_ID,
  surface_id: SURFACE_ID,
  kind: "assistant_text",
  ts: 1786138541.769324,
  seq: 28,
  status: "complete",
  payload: { text: "PONG" },
  run_ref: null,
  sidecar_ref: null,
  target_ref: null,
  child_manifest: null,
};

// Frame 6: the same assistant_text node re-upserted with parent_id now
// pointing at the thinking node's own id (the backend's mid-stream
// re-parent; not a recognized "explanation" container by node kind).
const ASSISTANT_TEXT_NODE_V2: NodeWire = { ...ASSISTANT_TEXT_NODE_V1, parent_id: THINKING_ID };

function upsert(node: NodeWire, render_rev: number): NodeUpsertFrame {
  return {
    cv: 1,
    surface_id: SURFACE_ID,
    snapshot: { incarnation: "cb1f8aacd352bfdf", render_rev, hist_rev: 0 },
    node,
    type: "node_upsert",
  };
}

// Wire order exactly as captured (ws-frames.json indices 49, 54, 92, 93,
// 130, 132, 135):
const FIXTURE_FRAMES: Array<NodeUpsertFrame | TurnLifecycleFrame> = [
  LIFECYCLE_FRAME,
  upsert(TYPED_PROMPT_NODE, 9),
  upsert(THINKING_NODE_V1, 26),
  upsert(ASSISTANT_TEXT_NODE_V1, 26),
  upsert(TYPED_PROMPT_NODE, 37),
  upsert(THINKING_NODE_V1, 37),
  upsert(ASSISTANT_TEXT_NODE_V2, 37),
];

describe("SurfaceStore live turn birth (state-machine tier)", () => {
  it("hydrates against an empty snapshot then renders PONG from the literal step2 evidence frames", async () => {
    fetchSnapshotImpl = () => Promise.resolve(EMPTY_SNAPSHOT("79fc3ce3-aed9-4474-97c8-052fa1a511ac"));
    const { SurfaceStore } = await import("../src/surface/state");
    const store = new SurfaceStore("79fc3ce3-aed9-4474-97c8-052fa1a511ac");

    await waitFor(() => expect(store.getSnapshot().hydrated).toBe(true));
    expect(store.getSnapshot().turnOrder).toEqual([]); // confirms the empty-snapshot precondition

    const socket = capturedSockets[0];
    for (const f of FIXTURE_FRAMES) socket.handlers.onFrame(f);

    const snap = store.getSnapshot();
    expect(snap.turnOrder).toEqual([TURN_ID]);
    const entry = snap.turnsById.get(TURN_ID)!;
    expect(entry.prompt?.kind).toBe("typed_prompt");
    // The lifecycle frame's mismatched turn_id must not have silently
    // stranded this turn at phase: null (isLivePhase(null) === false,
    // which alone would keep TurnView from ever rendering the body).
    expect(entry.phase).not.toBeNull();

    const bodyNodeIds = (store.getChildren(entry.turn.node_id) ?? []).map((n) => n.node_id);
    expect(bodyNodeIds).toContain(THINKING_ID);
    expect(bodyNodeIds).toContain(ASSISTANT_ID);
    store.dispose();
  });

  it("out-of-order: assistant_text delivered before its own turn's typed_prompt still births the turn and attaches both", async () => {
    fetchSnapshotImpl = () => Promise.resolve(EMPTY_SNAPSHOT("s-ooo"));
    const { SurfaceStore } = await import("../src/surface/state");
    const store = new SurfaceStore("s-ooo");
    await waitFor(() => expect(store.getSnapshot().hydrated).toBe(true));

    const socket = capturedSockets[0];
    // assistant_text arrives FIRST — no turn has been birthed for TURN_ID yet.
    socket.handlers.onFrame(upsert(ASSISTANT_TEXT_NODE_V1, 1));
    let snap = store.getSnapshot();
    expect(snap.turnOrder).toEqual([TURN_ID]); // birthed from the body node, not dropped
    expect(snap.turnsById.get(TURN_ID)?.prompt).toBeNull();

    // typed_prompt catches up afterward.
    socket.handlers.onFrame(upsert(TYPED_PROMPT_NODE, 2));
    snap = store.getSnapshot();
    expect(snap.turnsById.get(TURN_ID)?.prompt?.kind).toBe("typed_prompt");

    const bodyNodeIds = (store.getChildren(snap.turnsById.get(TURN_ID)!.turn.node_id) ?? []).map((n) => n.node_id);
    expect(bodyNodeIds).toContain(ASSISTANT_ID);
    store.dispose();
  });
});

describe("ChatSurfaceView live turn birth (component tier)", () => {
  it("renders the assistant text once the literal step2 evidence frames are delivered against an empty snapshot", async () => {
    fetchSnapshotImpl = () => Promise.resolve(EMPTY_SNAPSHOT("79fc3ce3-aed9-4474-97c8-052fa1a511ac"));
    const { ChatSurfaceView } = await import("../src/surface/ChatSurfaceView");
    render(<ChatSurfaceView sessionId="79fc3ce3-aed9-4474-97c8-052fa1a511ac" />);

    await waitFor(() => expect(capturedSockets.length).toBe(1));
    const socket = capturedSockets[0];
    for (const f of FIXTURE_FRAMES) socket.handlers.onFrame(f);

    // Asserted against the dedicated assistant-text node, NOT whole-tree
    // textContent — the prompt echo itself contains the literal word
    // "PONG" ("Reply with exactly the single word: PONG...."), so a
    // whole-tree substring check would pass even with zero body content
    // rendered.
    // AssistantTextView lazy-loads components/MessageBubble.tsx's
    // MessageBox (see nodes/ContentLeaves.tsx) — the wrapper div (and its
    // data-testid) mounts before that dynamic import resolves, and the
    // first import of that (large) module in a test run can outrun the
    // default waitFor timeout (same pattern as tests/surface/render.test.tsx).
    const assistantNode = await screen.findByTestId("surface-assistant-text");
    await waitFor(() => expect(assistantNode.textContent).toContain("PONG"), { timeout: 8000 });
  }, 10000);
});
