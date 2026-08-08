// Native send path (Phase I stage 2c): SurfaceStore.sendPrompt's
// optimistic-echo/reconcile/retry state machine, plus the DOM chrome
// TypedPromptView/PendingSendStatus render for it. Same fake-transport
// pattern as tests/surfaceStore-live-turn-birth.test.tsx (mocks
// ../src/adapter/client entirely) — isolated from the App/Chat.tsx
// composer wiring, which tests/client-id.test.ts covers end to end.

import { useSyncExternalStore } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {
  ChatIntentWire,
  NodeUpsertFrame,
  SnapshotEnvelope,
  TransportAckFrame,
} from "../../src/adapter/wire";

/** Overridden per test to control what a given session's `fetchSnapshot()`
 * resolves with — the provisional-send-survival tests below need a second
 * `hydrate()` (a fresh `SurfaceStore` for the SAME session id) to sometimes
 * already contain the backend-confirmed turn. Defaults to an empty
 * snapshot, same as the pre-existing tests' implicit assumption. */
let fetchSnapshotImpl: (sessionId: string) => SnapshotEnvelope = (sid) => EMPTY_SNAPSHOT(sid);

interface CapturedSocket {
  handlers: {
    onFrame: (frame: NodeUpsertFrame) => void;
    onResyncRequired: () => void;
    onOpen?: () => void;
    onClose?: () => void;
  };
  open: ReturnType<typeof vi.fn>;
  updateCursors: ReturnType<typeof vi.fn>;
  trackCursor: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  submit: (intent: ChatIntentWire) => Promise<TransportAckFrame> | null;
}

let capturedSockets: CapturedSocket[] = [];
/** Overridden per test to control what `submit()` returns/resolves. */
let submitImpl: (intent: ChatIntentWire) => Promise<TransportAckFrame> | null = () => null;
const submitCalls: ChatIntentWire[] = [];

vi.mock("../../src/adapter/client", () => {
  class FakeSurfaceClient {
    fetchSnapshot(sessionId: string) {
      return Promise.resolve(fetchSnapshotImpl(sessionId));
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
        submit: (intent: ChatIntentWire) => {
          submitCalls.push(intent);
          return submitImpl(intent);
        },
      };
      capturedSockets.push(socket);
      return socket;
    }
  }
  return { SurfaceClient: FakeSurfaceClient };
});

const IDENTITY = { incarnation: "mock", render_rev: 0, hist_rev: 0 };

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

beforeEach(() => {
  submitImpl = () => null;
  submitCalls.length = 0;
  fetchSnapshotImpl = (sid) => EMPTY_SNAPSHOT(sid);
});

afterEach(() => {
  cleanup();
  capturedSockets = [];
  vi.resetModules();
});

describe("SurfaceStore.sendPrompt (state-machine tier)", () => {
  it("inserts a provisional turn immediately and submits a send_prompt intent", async () => {
    submitImpl = () => new Promise(() => {}); // never resolves — assert the immediate state only
    const { SurfaceStore } = await import("../../src/surface/state");
    const store = new SurfaceStore("s1");
    await waitFor(() => expect(store.getSnapshot().hydrated).toBe(true));

    const intentId = store.sendPrompt("hello native", [], "queue");

    expect(submitCalls).toHaveLength(1);
    expect(submitCalls[0]).toEqual(
      expect.objectContaining({
        kind: "send_prompt",
        intent_id: intentId,
        session_id: "s1",
        text: "hello native",
        send_mode: "queue",
        target: { kind: "current", fork_node_id: null },
      }),
    );

    const snap = store.getSnapshot();
    expect(snap.turnOrder).toHaveLength(1);
    const entry = snap.turnsById.get(snap.turnOrder[0])!;
    expect(entry.prompt?.kind).toBe("typed_prompt");
    expect((entry.prompt!.payload as { text: string }).text).toBe("hello native");
    expect(entry.provisionalSend).toEqual({ intentId, status: "sending", errorMessage: null });
    store.dispose();
  });

  it("reconciles the provisional turn in place when the confirmed typed_prompt node arrives", async () => {
    submitImpl = () => Promise.resolve({ type: "intent_accepted", intent_id: "whatever" });
    const { SurfaceStore } = await import("../../src/surface/state");
    const store = new SurfaceStore("s1");
    await waitFor(() => expect(store.getSnapshot().hydrated).toBe(true));

    const intentId = store.sendPrompt("hello native", [], "queue");
    expect(store.getSnapshot().turnOrder).toHaveLength(1);

    const socket = capturedSockets[0];
    socket.handlers.onFrame({
      cv: 1,
      surface_id: "s1",
      snapshot: { incarnation: "mock", render_rev: 1, hist_rev: 0 },
      type: "node_upsert",
      node: {
        cv: 1,
        node_id: "real-prompt-1",
        parent_id: null,
        turn_id: "real-turn-1",
        surface_id: "s1",
        kind: "typed_prompt",
        ts: 100,
        seq: 0,
        status: null,
        payload: {
          text: "hello native",
          attachments: [],
          send_mode: "queue",
          origin: "user",
          source_session_ref: null,
          sent_text: null,
          intent_id: intentId,
        },
        run_ref: null,
        sidecar_ref: null,
        target_ref: null,
        child_manifest: null,
      },
    });

    const snap = store.getSnapshot();
    // Still exactly one turn — the provisional was replaced, not duplicated.
    expect(snap.turnOrder).toEqual(["real-turn-1"]);
    const entry = snap.turnsById.get("real-turn-1")!;
    expect(entry.provisionalSend).toBeNull();
    expect(entry.prompt?.node_id).toBe("real-prompt-1");
    store.dispose();
  });

  it("flips to an error state when the backend rejects the intent", async () => {
    let resolveAck!: (ack: TransportAckFrame) => void;
    submitImpl = () => new Promise((resolve) => { resolveAck = resolve; });
    const { SurfaceStore } = await import("../../src/surface/state");
    const store = new SurfaceStore("s1");
    await waitFor(() => expect(store.getSnapshot().hydrated).toBe(true));

    const intentId = store.sendPrompt("will be rejected", [], "queue");
    resolveAck({ type: "intent_rejected", intent_id: intentId, code: "rejected", message: "no session selected" });
    await waitFor(() => {
      const turnId = store.getSnapshot().turnOrder[0];
      expect(store.getSnapshot().turnsById.get(turnId)!.provisionalSend?.status).toBe("error");
    });

    const turnId = store.getSnapshot().turnOrder[0];
    expect(store.getSnapshot().turnsById.get(turnId)!.provisionalSend).toEqual({
      intentId,
      status: "error",
      errorMessage: "no session selected",
    });
    store.dispose();
  });

  it("fails immediately (synchronously) when the socket is not open — no client-side queueing", async () => {
    submitImpl = () => null; // SurfaceSocket.submit's own not-OPEN contract
    const { SurfaceStore } = await import("../../src/surface/state");
    const store = new SurfaceStore("s1");
    await waitFor(() => expect(store.getSnapshot().hydrated).toBe(true));

    const intentId = store.sendPrompt("offline attempt", [], "queue");
    const turnId = store.getSnapshot().turnOrder[0];
    expect(store.getSnapshot().turnsById.get(turnId)!.provisionalSend).toEqual({
      intentId,
      status: "error",
      errorMessage: "not connected",
    });
    store.dispose();
  });

  it("retrySend replaces the failed entry with a fresh intent_id, preserving text/sendMode", async () => {
    submitImpl = () => null;
    const { SurfaceStore } = await import("../../src/surface/state");
    const store = new SurfaceStore("s1");
    await waitFor(() => expect(store.getSnapshot().hydrated).toBe(true));

    const firstId = store.sendPrompt("retry me", [], "steer");
    expect(store.getSnapshot().turnOrder).toHaveLength(1);

    const secondId = store.retrySend(firstId);
    expect(secondId).not.toBeNull();
    expect(secondId).not.toBe(firstId);

    const snap = store.getSnapshot();
    expect(snap.turnOrder).toHaveLength(1); // old failed entry removed, not appended alongside
    const entry = snap.turnsById.get(snap.turnOrder[0])!;
    expect(entry.provisionalSend?.intentId).toBe(secondId);
    expect(entry.provisionalSend?.status).toBe("error"); // socket still closed
    expect((entry.prompt!.payload as { text: string; send_mode: string }).text).toBe("retry me");
    expect((entry.prompt!.payload as { text: string; send_mode: string }).send_mode).toBe("steer");
    expect(submitCalls).toHaveLength(2);
    store.dispose();
  });

  it("retrySend is a no-op for an id that isn't a currently-failed provisional entry", async () => {
    const { SurfaceStore } = await import("../../src/surface/state");
    const store = new SurfaceStore("s1");
    await waitFor(() => expect(store.getSnapshot().hydrated).toBe(true));
    expect(store.retrySend("no-such-intent")).toBeNull();
    store.dispose();
  });
});

describe("SurfaceStore provisional-send survival across session switch", () => {
  // `useSurfaceStore.ts` disposes and recreates a fresh `SurfaceStore` on
  // every session switch — these simulate that lifecycle directly (no
  // React/hook layer needed, `SurfaceStore` doesn't know about React) by
  // constructing a second store for the SAME session id after `dispose()`.

  it("a pending ('sending') provisional send survives disposing and recreating the store", async () => {
    submitImpl = () => new Promise(() => {}); // never resolves — stays "sending"
    const { SurfaceStore } = await import("../../src/surface/state");
    const store1 = new SurfaceStore("s1");
    await waitFor(() => expect(store1.getSnapshot().hydrated).toBe(true));

    const intentId = store1.sendPrompt("switch away", [], "queue");
    expect(store1.getSnapshot().turnOrder).toHaveLength(1);
    store1.dispose(); // simulates useSurfaceStore.ts's teardown on session switch

    const store2 = new SurfaceStore("s1"); // simulates switching back
    await waitFor(() => expect(store2.getSnapshot().hydrated).toBe(true));

    const snap = store2.getSnapshot();
    expect(snap.turnOrder).toHaveLength(1);
    const entry = snap.turnsById.get(snap.turnOrder[0])!;
    expect(entry.provisionalSend).toEqual({ intentId, status: "sending", errorMessage: null });
    expect((entry.prompt!.payload as { text: string }).text).toBe("switch away");
    store2.dispose();
  });

  it("a failed ('error', Retry-able) provisional send also survives a session switch", async () => {
    submitImpl = () => null; // socket not open — immediate "not connected" error
    const { SurfaceStore } = await import("../../src/surface/state");
    const store1 = new SurfaceStore("s1");
    await waitFor(() => expect(store1.getSnapshot().hydrated).toBe(true));

    const intentId = store1.sendPrompt("failed before switch", [], "queue");
    expect(
      store1.getSnapshot().turnsById.get(store1.getSnapshot().turnOrder[0])!.provisionalSend?.status,
    ).toBe("error");
    store1.dispose();

    const store2 = new SurfaceStore("s1");
    await waitFor(() => expect(store2.getSnapshot().hydrated).toBe(true));

    const entry = store2.getSnapshot().turnsById.get(store2.getSnapshot().turnOrder[0])!;
    expect(entry.provisionalSend).toEqual({ intentId, status: "error", errorMessage: "not connected" });
    store2.dispose();
  });

  it("reconciles against the fresh snapshot when the confirming typed_prompt arrived while the session was not active", async () => {
    submitImpl = () => new Promise(() => {}); // never acks on store1 — switched away first
    const { SurfaceStore } = await import("../../src/surface/state");
    const store1 = new SurfaceStore("s1");
    await waitFor(() => expect(store1.getSnapshot().hydrated).toBe(true));

    const intentId = store1.sendPrompt("resolved while away", [], "queue");
    store1.dispose(); // socket closed — the real confirming node_upsert can never reach store1 live

    // The backend finished processing it while this session was inactive —
    // the next hydrate (switching back) already carries the confirmed turn.
    fetchSnapshotImpl = (sid) => ({
      kind: "ok",
      snapshot_identity: IDENTITY,
      session_id: sid,
      surface_id: sid,
      instruction_widget: null,
      turns: [
        {
          turn: {
            cv: 1,
            node_id: "turn:real-1",
            parent_id: null,
            turn_id: "real-1",
            surface_id: sid,
            kind: "turn",
            ts: 100,
            seq: 0,
            status: null,
            payload: null,
            run_ref: null,
            sidecar_ref: null,
            target_ref: null,
            child_manifest: null,
          },
          prompt: {
            cv: 1,
            node_id: "real-prompt-1",
            parent_id: null,
            turn_id: "real-1",
            surface_id: sid,
            kind: "typed_prompt",
            ts: 100,
            seq: 0,
            status: null,
            payload: {
              text: "resolved while away",
              attachments: [],
              send_mode: "queue",
              origin: "user",
              source_session_ref: null,
              sent_text: null,
              intent_id: intentId,
            },
            run_ref: null,
            sidecar_ref: null,
            target_ref: null,
            child_manifest: null,
          },
          results: [],
          manifest: { renderable_child_count: 0, has_children: false },
          runtime_change: null,
        },
      ],
      live_turn_nodes: [],
      runs: [],
      older_cursor: null,
    });

    const store2 = new SurfaceStore("s1");
    await waitFor(() => expect(store2.getSnapshot().hydrated).toBe(true));

    const snap = store2.getSnapshot();
    // Exactly the one real turn — no duplicate provisional re-inserted
    // alongside it.
    expect(snap.turnOrder).toEqual(["real-1"]);
    const entry = snap.turnsById.get("real-1")!;
    expect(entry.provisionalSend).toBeNull();
    expect(entry.prompt?.node_id).toBe("real-prompt-1");
    store2.dispose();

    // The registry record was actually cleared (not just shadowed by the
    // snapshot match above) — a THIRD store for the same session, hydrated
    // from an empty snapshot again, must show zero leftover provisional
    // turns.
    fetchSnapshotImpl = (sid) => EMPTY_SNAPSHOT(sid);
    const store3 = new SurfaceStore("s1");
    await waitFor(() => expect(store3.getSnapshot().hydrated).toBe(true));
    expect(store3.getSnapshot().turnOrder).toHaveLength(0);
    store3.dispose();
  });

  it("does not leak a provisional send into a DIFFERENT session's store", async () => {
    submitImpl = () => new Promise(() => {});
    const { SurfaceStore } = await import("../../src/surface/state");
    const storeA = new SurfaceStore("session-a");
    await waitFor(() => expect(storeA.getSnapshot().hydrated).toBe(true));
    storeA.sendPrompt("only for A", [], "queue");
    expect(storeA.getSnapshot().turnOrder).toHaveLength(1);

    const storeB = new SurfaceStore("session-b");
    await waitFor(() => expect(storeB.getSnapshot().hydrated).toBe(true));
    expect(storeB.getSnapshot().turnOrder).toHaveLength(0);

    storeA.dispose();
    storeB.dispose();
  });
});

describe("PendingSendStatus chrome (component tier, via TurnView)", () => {
  it("shows a sending indicator immediately, then an error+Retry after rejection", async () => {
    let resolveAck!: (ack: TransportAckFrame) => void;
    submitImpl = () => new Promise((resolve) => { resolveAck = resolve; });
    const { SurfaceStore } = await import("../../src/surface/state");
    const { TurnView } = await import("../../src/surface/TurnView");
    const store = new SurfaceStore("s1");
    await waitFor(() => expect(store.getSnapshot().hydrated).toBe(true));

    const intentId = store.sendPrompt("chrome check", [], "queue");

    function Harness() {
      // `turnOrder[0]` (not a captured turnId) — retry below replaces the
      // provisional turn under a fresh id, same as `reconcileProvisionalSend`
      // does on success; this harness always has exactly one turn.
      const snap = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
      const entry = snap.turnsById.get(snap.turnOrder[0]);
      if (!entry) return null;
      return <TurnView entry={entry} store={store} runsById={new Map()} />;
    }
    render(<Harness />);

    expect((await screen.findByTestId("surface-prompt-status")).className).toContain("status-sending");

    resolveAck({ type: "intent_rejected", intent_id: intentId, code: "rejected", message: "boom" });
    await waitFor(() =>
      expect(screen.getByTestId("surface-prompt-status").className).toContain("status-error"),
    );
    expect(screen.getByText("Retry")).toBeTruthy();

    const user = userEvent.setup();
    await user.click(screen.getByText("Retry"));
    // Retry re-inserts a fresh "sending" entry — old failed one is gone.
    await waitFor(() =>
      expect(screen.getByTestId("surface-prompt-status").className).toContain("status-sending"),
    );
    expect(submitCalls).toHaveLength(2);

    store.dispose();
  });
});
