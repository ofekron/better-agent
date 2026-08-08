// Grammar/contract rules under test (surface/state.ts):
//  - hydrate() builds turnOrder/turnsById/runsById from the snapshot and
//    eager-seeds the trailing turn's children tables from live_turn_nodes.
//  - node_upsert/text_delta/node_status/turn_lifecycle frames route
//    correctly (typed_prompt/result/turn/model_change special-cased,
//    everything else -> the owning container's children table).
//  - loadOlder() prepends without touching existing turns (paging).
//  - ensureChildren() dedupes concurrent fetches and caches the result.

import { act, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isLivePhase, SurfaceStore } from "../../src/surface/state";
import { MockWebSocketController } from "../harness/mockWebSocket";
import {
  assistantTextNode,
  compactTurn,
  explanationNode,
  jsonResponse,
  promptNode,
  resetSeq,
  resultNode,
  runWire,
  SESSION,
  snapshotEnvelope,
  turnNode,
} from "./fixtures";
import type { WSEvent } from "../../src/types";

describe("isLivePhase", () => {
  // Regression for A0-1: the backend's TurnPhase enum only ever sends
  // "awaiting_interaction" (surface_contract/frames.py) — AWAITING_APPROVAL
  // was renamed away. LIVE_PHASES must key off the current wire value.
  it("treats awaiting_interaction as live", () => {
    expect(isLivePhase("awaiting_interaction")).toBe(true);
  });

  it("treats every non-terminal phase as live, terminal phases and null as not-live", () => {
    for (const phase of ["queued", "starting", "running", "reconnecting", "stopping"] as const) {
      expect(isLivePhase(phase)).toBe(true);
    }
    for (const phase of ["completed", "stopped", "failed"] as const) {
      expect(isLivePhase(phase)).toBe(false);
    }
    expect(isLivePhase(null)).toBe(false);
  });
});

describe("SurfaceStore", () => {
  let ws: MockWebSocketController;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    resetSeq();
    ws = new MockWebSocketController();
    ws.install();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    ws.uninstall();
    vi.unstubAllGlobals();
  });

  it("hydrates turnOrder/turnsById/runsById from the snapshot", async () => {
    const t1 = turnNode("t1");
    const envelope = snapshotEnvelope(
      [compactTurn(t1, promptNode("t1", "hi"), [resultNode("t1", "ok")])],
      [],
      { runs: [runWire("r1")] },
    );
    fetchMock.mockImplementation(() => jsonResponse(envelope));
    const store = new SurfaceStore(SESSION);

    await waitFor(() => expect(store.getSnapshot().hydrated).toBe(true));
    const snap = store.getSnapshot();
    expect(snap.turnOrder).toEqual(["t1"]);
    expect(snap.turnsById.get("t1")?.prompt?.payload).toMatchObject({ text: "hi" });
    expect(snap.turnsById.get("t1")?.results[0]?.payload).toMatchObject({ text: "ok" });
    expect(snap.runsById.get("r1")).toEqual(runWire("r1"));
    store.dispose();
  });

  it("eager-seeds the trailing turn's children table from live_turn_nodes, bucketed by parent_id", async () => {
    const t1 = turnNode("t1", { renderable_child_count: 1, has_children: true });
    const exp = explanationNode("t1", "exp1", "turn:t1", { renderable_child_count: 1, has_children: true });
    const member = assistantTextNode("t1", "m1", "hello", "exp1", "streaming");
    const envelope = snapshotEnvelope(
      [compactTurn(t1, promptNode("t1", "hi"), [])],
      [t1, exp, member],
    );
    fetchMock.mockImplementation(() => jsonResponse(envelope));
    const store = new SurfaceStore(SESSION);
    await waitFor(() => expect(store.getSnapshot().hydrated).toBe(true));

    // Seeded without any children() REST call.
    expect(store.getChildren("turn:t1")).toEqual([exp]);
    expect(store.getChildren("exp1")).toEqual([member]);
    expect(fetchMock).toHaveBeenCalledTimes(1); // only the snapshot fetch
    store.dispose();
  });

  it("routes a live node_upsert into its parent's already-materialized children table", async () => {
    const t1 = turnNode("t1", { renderable_child_count: 1, has_children: true });
    const exp = explanationNode("t1", "exp1", "turn:t1", { renderable_child_count: 1, has_children: true });
    const member = assistantTextNode("t1", "m1", "hello", "exp1");
    const envelope = snapshotEnvelope([compactTurn(t1, promptNode("t1", "hi"), [])], [t1, exp, member]);
    fetchMock.mockImplementation(() => jsonResponse(envelope));
    const store = new SurfaceStore(SESSION);
    await waitFor(() => expect(store.getSnapshot().hydrated).toBe(true));

    const newMember = assistantTextNode("t1", "m2", "world", "exp1");
    act(() => {
      ws.emit({
        type: "node_upsert",
        cv: 1,
        surface_id: SESSION,
        snapshot: { incarnation: "inc-1", render_rev: 1, hist_rev: 0 },
        node: newMember,
      } as unknown as WSEvent);
    });

    await waitFor(() => expect(store.getChildren("exp1")).toHaveLength(2));
    expect(store.getChildren("exp1")?.map((n) => n.node_id)).toEqual(["m1", "m2"]);
    store.dispose();
  });

  it("applies text_delta only to assistant_text/thinking nodes, appending in place", async () => {
    const t1 = turnNode("t1", { renderable_child_count: 1, has_children: true });
    const exp = explanationNode("t1", "exp1", "turn:t1", { renderable_child_count: 1, has_children: true });
    const member = assistantTextNode("t1", "m1", "hel", "exp1", "streaming");
    const envelope = snapshotEnvelope([compactTurn(t1, promptNode("t1", "hi"), [])], [t1, exp, member]);
    fetchMock.mockImplementation(() => jsonResponse(envelope));
    const store = new SurfaceStore(SESSION);
    await waitFor(() => expect(store.getSnapshot().hydrated).toBe(true));

    act(() => {
      ws.emit({
        type: "text_delta",
        cv: 1,
        surface_id: SESSION,
        snapshot: { incarnation: "inc-1", render_rev: 1, hist_rev: 0 },
        node_id: "m1",
        appended_text: "lo",
      } as unknown as WSEvent);
    });

    await waitFor(() => {
      const n = store.getChildren("exp1")?.find((x) => x.node_id === "m1");
      expect((n?.payload as { text: string }).text).toBe("hello");
    });
    store.dispose();
  });

  it("turn_lifecycle updates the owning TurnEntry's phase/reason/usage", async () => {
    const t1 = turnNode("t1");
    const envelope = snapshotEnvelope([compactTurn(t1, promptNode("t1", "hi"), [])]);
    fetchMock.mockImplementation(() => jsonResponse(envelope));
    const store = new SurfaceStore(SESSION);
    await waitFor(() => expect(store.getSnapshot().hydrated).toBe(true));
    expect(store.getSnapshot().turnsById.get("t1")?.phase).toBeNull();

    act(() => {
      ws.emit({
        type: "turn_lifecycle",
        cv: 1,
        surface_id: SESSION,
        snapshot: { incarnation: "inc-1", render_rev: 1, hist_rev: 0 },
        turn_id: "t1",
        phase: "running",
        reason: null,
        usage: null,
      } as unknown as WSEvent);
    });

    await waitFor(() => expect(store.getSnapshot().turnsById.get("t1")?.phase).toBe("running"));
    store.dispose();
  });

  it("loadOlder() prepends without duplicating or disturbing already-loaded turns", async () => {
    const t2 = turnNode("t2");
    const envelope = snapshotEnvelope(
      [compactTurn(t2, promptNode("t2", "second"), [])],
      [],
      { olderCursor: "cursor-1" },
    );
    fetchMock.mockImplementation((url: string) => {
      const u = String(url);
      if (u.includes("/older")) {
        const t1 = turnNode("t1");
        return jsonResponse({
          kind: "ok",
          snapshot_identity: { incarnation: "inc-1", render_rev: 0, hist_rev: 0 },
          turns: [compactTurn(t1, promptNode("t1", "first"), [])],
          runs: [],
          older_cursor: null,
        });
      }
      return jsonResponse(envelope);
    });
    const store = new SurfaceStore(SESSION);
    await waitFor(() => expect(store.getSnapshot().hydrated).toBe(true));
    expect(store.getSnapshot().turnOrder).toEqual(["t2"]);

    await store.loadOlder();
    expect(store.getSnapshot().turnOrder).toEqual(["t1", "t2"]);
    expect(store.getSnapshot().olderCursor).toBeNull();
    store.dispose();
  });

  it("ensureChildren() dedupes concurrent callers into one REST request", async () => {
    const t1 = turnNode("t1", { renderable_child_count: 1, has_children: true });
    const envelope = snapshotEnvelope([compactTurn(t1, promptNode("t1", "hi"), [])]);
    let childrenCalls = 0;
    fetchMock.mockImplementation((url: string) => {
      const u = String(url);
      if (u.includes("/children")) {
        childrenCalls++;
        return jsonResponse({
          kind: "ok",
          value: [explanationNode("t1", "exp1", "turn:t1", { renderable_child_count: 0, has_children: false })],
          snapshot_identity: { incarnation: "inc-1", render_rev: 0, hist_rev: 0 },
        });
      }
      return jsonResponse(envelope);
    });
    const store = new SurfaceStore(SESSION);
    await waitFor(() => expect(store.getSnapshot().hydrated).toBe(true));

    const [a, b] = await Promise.all([store.ensureChildren("turn:t1"), store.ensureChildren("turn:t1")]);
    expect(childrenCalls).toBe(1);
    expect(a).toEqual(b);
    store.dispose();
  });
});
