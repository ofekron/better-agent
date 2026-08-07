// Repro for the live-validation finding (session d318331f-*): with
// ba.surface_native=1, ChatSurfaceView mounts (data-testid="surface-chat-view"
// present) but stays empty for the whole run while /ws/v2/surface delivers
// correct node_upsert frames (typed_prompt, thinking, assistant_text PONG —
// see .../live-validation/d318331f-*.v2-content-frames.json, reproduced
// verbatim below as FIXTURE_FRAMES).
//
// Two tiers:
//   1. State-machine only (SurfaceStore.onFrame path) — isolates the store's
//      own frame-handling logic from React wiring.
//   2. Full component (ChatSurfaceView) — additionally exercises
//      useSurfaceStore's useSyncExternalStore wiring.

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import type { NodeWire, NodeUpsertFrame, SnapshotEnvelope } from "../src/adapter/wire";

// ---- Fake transport ------------------------------------------------------

interface CapturedSocket {
  handlers: { onFrame: (frame: NodeUpsertFrame) => void; onResyncRequired: () => void };
  open: ReturnType<typeof vi.fn>;
  updateCursors: ReturnType<typeof vi.fn>;
  trackCursor: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
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
    openSocket(handlers: CapturedSocket["handlers"]) {
      const socket: CapturedSocket = {
        handlers,
        open: vi.fn(),
        updateCursors: vi.fn(),
        trackCursor: vi.fn(),
        close: vi.fn(),
      };
      capturedSockets.push(socket);
      return socket;
    }
  }
  return { SurfaceClient: FakeSurfaceClient };
});

const IDENTITY = { incarnation: "b6bb240818d7909a", render_rev: 0, hist_rev: 0 };

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

// ---- Fixture frames (verbatim shape from d318331f-*.v2-content-frames.json) ----

const SURFACE_ID = "d318331f-cd96-4672-bac9-c08df1075f20";
const TURN_ID = "2befa976-92d9-41a9-ab48-e1d84c6bd42a";

function frame(node: NodeWire, render_rev: number): NodeUpsertFrame {
  return {
    cv: 1,
    surface_id: SURFACE_ID,
    snapshot: { incarnation: "b6bb240818d7909a", render_rev, hist_rev: 0 },
    node,
    type: "node_upsert",
  };
}

const TYPED_PROMPT_FRAME = frame(
  {
    cv: 1,
    node_id: TURN_ID,
    parent_id: null,
    turn_id: TURN_ID,
    surface_id: SURFACE_ID,
    kind: "typed_prompt",
    ts: 1786140134.115265,
    seq: 10,
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
  },
  10,
);

const THINKING_FRAME = frame(
  {
    cv: 1,
    node_id: "cb6f0939-2198-4c9e-a6e0-ab69d2bdbf1a",
    parent_id: null,
    turn_id: TURN_ID,
    surface_id: SURFACE_ID,
    kind: "thinking",
    ts: 1786140148.121881,
    seq: 30,
    status: "complete",
    payload: {
      text: "The user has asked me to reply with exactly the single word: PONG.",
      redacted: false,
    },
    run_ref: null,
    sidecar_ref: null,
    target_ref: null,
    child_manifest: null,
  },
  28,
);

const ASSISTANT_TEXT_FRAME = frame(
  {
    cv: 1,
    node_id: "03d2c7e4-0138-4b9a-96d8-c73b189e582f",
    parent_id: null,
    turn_id: TURN_ID,
    surface_id: SURFACE_ID,
    kind: "assistant_text",
    ts: 1786140148.158455,
    seq: 31,
    status: "complete",
    payload: { text: "PONG" },
    run_ref: null,
    sidecar_ref: null,
    target_ref: null,
    child_manifest: null,
  },
  29,
);

afterEach(() => {
  cleanup();
  capturedSockets = [];
  vi.resetModules();
});

describe("SurfaceStore native frame handling (state-machine tier)", () => {
  it("builds a turn from a live typed_prompt+thinking+assistant_text sequence delivered AFTER an empty hydrate", async () => {
    fetchSnapshotImpl = () => Promise.resolve(EMPTY_SNAPSHOT("s1"));
    const { SurfaceStore } = await import("../src/surface/state");
    const store = new SurfaceStore("s1");

    // Let the (fake, already-resolved) hydrate() promise settle before the
    // socket "connects" — mirrors production: syncSocketCursor()/socket.open()
    // only happens once hydrate() sets `identity`.
    await waitFor(() => expect(store.getSnapshot().hydrated).toBe(true));
    expect(capturedSockets.length).toBe(1);
    const socket = capturedSockets[0];
    expect(socket.open).toHaveBeenCalledTimes(1);

    socket.handlers.onFrame(TYPED_PROMPT_FRAME);
    socket.handlers.onFrame(THINKING_FRAME);
    socket.handlers.onFrame(ASSISTANT_TEXT_FRAME);

    const snap = store.getSnapshot();
    expect(snap.turnOrder.length).toBe(1);
    const entry = snap.turnsById.get(TURN_ID);
    expect(entry?.prompt?.kind).toBe("typed_prompt");
  });
});

describe("Native + down-map integration (dual-consumer regression)", () => {
  it("opens exactly one /ws/v2/surface consumer for a session once native is on", async () => {
    fetchSnapshotImpl = () => Promise.resolve(EMPTY_SNAPSHOT("s1"));
    const [{ ChatSurfaceView }, { useSurfaceSession }, { resolveSurfaceSessionId }] = await Promise.all([
      import("../src/surface/ChatSurfaceView"),
      import("../src/adapter/useSurfaceSession"),
      import("../src/hooks/useSession"),
    ]);

    function Harness() {
      // Mirrors useSession.ts's real wiring: the down-map hook's
      // sessionId is gated through resolveSurfaceSessionId exactly like
      // the production `surfaceSessionId` — native being on must disable
      // it, leaving ChatSurfaceView's own SurfaceStore as the sole
      // `/ws/v2/surface` consumer for this session.
      const surfaceSessionId = resolveSurfaceSessionId("s1", true, true);
      useSurfaceSession({ sessionId: surfaceSessionId, onSnapshot: () => {}, onUpsertMessage: () => {} });
      return <ChatSurfaceView sessionId="s1" />;
    }

    render(<Harness />);
    await screen.findByTestId("surface-chat-view");

    // Give any (incorrect) second effect a chance to fire before asserting.
    await waitFor(() => expect(capturedSockets.length).toBeGreaterThan(0));
    expect(capturedSockets.length).toBe(1);
  });
});

describe("ChatSurfaceView native rendering (component tier)", () => {
  it("renders the assistant text once the store's frames are delivered", async () => {
    fetchSnapshotImpl = () => Promise.resolve(EMPTY_SNAPSHOT("s1"));
    const { ChatSurfaceView } = await import("../src/surface/ChatSurfaceView");
    render(<ChatSurfaceView sessionId="s1" />);

    await waitFor(() => expect(capturedSockets.length).toBe(1));
    const socket = capturedSockets[0];

    socket.handlers.onFrame(TYPED_PROMPT_FRAME);
    socket.handlers.onFrame(THINKING_FRAME);
    socket.handlers.onFrame(ASSISTANT_TEXT_FRAME);

    const root = await screen.findByTestId("surface-chat-view");
    await waitFor(() => expect(root.textContent).toContain("PONG"));
  });
});
