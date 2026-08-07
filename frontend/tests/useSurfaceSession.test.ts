import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSurfaceSession } from "../src/adapter/useSurfaceSession";
import type { CompactSessionSnapshotWire, NodeWire } from "../src/adapter/wire";
import type { ChatMessage } from "../src/types";
import { MockWebSocketController } from "./harness/mockWebSocket";

const SESSION = "s1";

function promptNode(turnId: string, text: string, ts: number): NodeWire {
  return {
    cv: 1, node_id: turnId, parent_id: null, turn_id: turnId, surface_id: SESSION,
    kind: "typed_prompt", ts, seq: 0, status: "complete",
    payload: { text, attachments: [], send_mode: "queue", origin: "user", source_session_ref: null, sent_text: null, intent_id: null },
    run_ref: null, sidecar_ref: null, target_ref: null, child_manifest: null,
  };
}

function resultNode(turnId: string, ts: number): NodeWire {
  return {
    cv: 1, node_id: `${turnId}:result`, parent_id: null, turn_id: turnId, surface_id: SESSION,
    kind: "result", ts, seq: 1, status: null, payload: { result_kind: "provider", text: null, is_error: false },
    run_ref: null, sidecar_ref: null, target_ref: null, child_manifest: null,
  };
}

function turnNode(turnId: string, ts: number): NodeWire {
  return {
    cv: 1, node_id: `turn:${turnId}`, parent_id: null, turn_id: turnId, surface_id: SESSION,
    kind: "turn", ts, seq: 0, status: null, payload: null,
    run_ref: null, sidecar_ref: null, target_ref: null,
    child_manifest: { renderable_child_count: 0, has_children: false },
  };
}

function snapshotBody(incarnation: string, renderRev: number, turnId: string, ts: number) {
  const snapshot: CompactSessionSnapshotWire = {
    session_id: SESSION, surface_id: SESSION, instruction_widget: null,
    turns: [
      {
        turn: turnNode(turnId, ts), prompt: promptNode(turnId, "hi", ts),
        results: [resultNode(turnId, ts)],
        manifest: { renderable_child_count: 0, has_children: false }, runtime_change: null,
      },
    ],
    live_turn_nodes: [],
    runs: [],
    older_cursor: null,
  };
  return {
    ...snapshot,
    kind: "ok" as const,
    snapshot_identity: { incarnation, render_rev: renderRev, hist_rev: 0 },
  };
}

describe("useSurfaceSession", () => {
  let ws: MockWebSocketController;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    ws = new MockWebSocketController();
    ws.install();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    ws.uninstall();
    vi.unstubAllGlobals();
  });

  function jsonResponse(body: unknown) {
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body) } as Response);
  }

  it("does nothing when sessionId is null (the OFF path)", () => {
    const onSnapshot = vi.fn();
    const onUpsertMessage = vi.fn();
    renderHook(() => useSurfaceSession({ sessionId: null, onSnapshot, onUpsertMessage }));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(onSnapshot).not.toHaveBeenCalled();
  });

  it("hydrates via fetchSnapshot and pushes a full replace through onSnapshot", async () => {
    fetchMock.mockImplementation((url: string) => {
      expect(String(url)).toContain(`/api/v2/surface/sessions/${SESSION}/snapshot`);
      return jsonResponse(snapshotBody("inc-1", 0, "turn-a", 100));
    });
    const onSnapshot = vi.fn();
    const onUpsertMessage = vi.fn();
    renderHook(() => useSurfaceSession({ sessionId: SESSION, onSnapshot, onUpsertMessage }));

    await waitFor(() => expect(onSnapshot).toHaveBeenCalledTimes(1));
    const [sid, messages] = onSnapshot.mock.calls[0] as [string, ChatMessage[]];
    expect(sid).toBe(SESSION);
    expect(messages.map((m) => m.role)).toEqual(["user", "assistant"]);
  });

  it("opens the v2 surface socket and subscribes with the hydrated cursor", async () => {
    fetchMock.mockImplementation(() => jsonResponse(snapshotBody("inc-1", 0, "turn-a", 100)));
    renderHook(() => useSurfaceSession({ sessionId: SESSION, onSnapshot: vi.fn(), onUpsertMessage: vi.fn() }));

    await waitFor(() => expect(ws.outbound.length).toBeGreaterThan(0));
    const subscribe = ws.outbound[0] as { surfaces: Array<{ surface_id: string; incarnation: string; render_rev: number }> };
    expect(subscribe.surfaces).toEqual([{ surface_id: SESSION, incarnation: "inc-1", render_rev: 0 }]);
  });

  it("upserts a single ChatMessage on a node_upsert live frame instead of replacing the whole snapshot", async () => {
    fetchMock.mockImplementation(() => jsonResponse(snapshotBody("inc-1", 0, "turn-a", 100)));
    const onSnapshot = vi.fn();
    const onUpsertMessage = vi.fn();
    renderHook(() => useSurfaceSession({ sessionId: SESSION, onSnapshot, onUpsertMessage }));
    await waitFor(() => expect(onSnapshot).toHaveBeenCalledTimes(1));

    act(() => {
      ws.emit({
        type: "node_upsert",
        cv: 1, surface_id: SESSION, snapshot: { incarnation: "inc-1", render_rev: 1, hist_rev: 0 },
        node: {
          cv: 1, node_id: "b1", parent_id: null, turn_id: "turn-a", surface_id: SESSION,
          kind: "assistant_text", ts: 101, seq: 5, status: "complete",
          payload: { text: "live update" }, run_ref: null, sidecar_ref: null, target_ref: null, child_manifest: null,
        },
      } as unknown as import("../src/types").WSEvent);
    });

    await waitFor(() => expect(onUpsertMessage).toHaveBeenCalled());
    expect(onSnapshot).toHaveBeenCalledTimes(1); // no extra wholesale replace
    const [, msg] = onUpsertMessage.mock.calls.at(-1) as [string, ChatMessage];
    expect(msg.role).toBe("assistant");
    expect(msg.events.some((e) => e.type === "output" && e.data.output === "live update")).toBe(true);
  });

  it("lazily fetches a native_subagent_turn's children via the children() endpoint and re-derives once resolved", async () => {
    fetchMock.mockImplementation((url: string) => {
      const u = String(url);
      if (u.includes("/snapshot")) return jsonResponse(snapshotBody("inc-1", 0, "turn-a", 100));
      if (u.includes("/nodes/subagent%3Atool%3Ax/children")) {
        return jsonResponse({
          kind: "ok",
          value: [
            {
              cv: 1, node_id: "sc1", parent_id: null, turn_id: "turn-a", surface_id: SESSION,
              kind: "assistant_text", ts: 1, seq: 0, status: "complete",
              payload: { text: "subagent says hi" }, run_ref: null, sidecar_ref: null, target_ref: null, child_manifest: null,
            },
          ],
          snapshot_identity: { incarnation: "inc-1", render_rev: 0, hist_rev: 0 },
        });
      }
      throw new Error(`unexpected fetch: ${u}`);
    });
    const onSnapshot = vi.fn();
    const onUpsertMessage = vi.fn();
    renderHook(() => useSurfaceSession({ sessionId: SESSION, onSnapshot, onUpsertMessage }));
    await waitFor(() => expect(onSnapshot).toHaveBeenCalledTimes(1));

    act(() => {
      ws.emit({
        type: "node_upsert",
        cv: 1, surface_id: SESSION, snapshot: { incarnation: "inc-1", render_rev: 1, hist_rev: 0 },
        node: {
          cv: 1, node_id: "subagent:tool:x", parent_id: null, turn_id: "turn-a", surface_id: SESSION,
          kind: "native_subagent_turn", ts: 101, seq: 5, status: "complete", payload: null,
          run_ref: null, sidecar_ref: null, target_ref: null,
          child_manifest: { renderable_child_count: 1, has_children: true },
        },
      } as unknown as import("../src/types").WSEvent);
    });

    // First upsert (structural node arrives, children not fetched yet):
    // contributes nothing to events. This is the SYNCHRONOUS push from the
    // node_upsert handler itself, guaranteed to run before the async
    // children() fetch's `.then()` callback can — so it's asserted on
    // `calls[0]` specifically, not the latest call (which `waitFor`'s
    // polling can easily race past the fetch's resolution by the time it
    // returns).
    await waitFor(() => expect(onUpsertMessage).toHaveBeenCalled());
    const beforeFetch = onUpsertMessage.mock.calls[0][1] as ChatMessage;
    expect(beforeFetch.events.some((e) => e.data?.node_id === "sc1")).toBe(false);

    // The async children() fetch resolves -> a SECOND upsert with the
    // resolved child event, tagged with the anchor's tool_use_id.
    await waitFor(() =>
      expect(onUpsertMessage.mock.calls.some(([, m]: [string, ChatMessage]) =>
        m.events.some((e) => e.data?.node_id === "sc1"),
      )).toBe(true),
    );
    const resolved = onUpsertMessage.mock.calls.at(-1)![1] as ChatMessage;
    const childEvent = resolved.events.find((e) => e.data?.node_id === "sc1");
    expect(childEvent).toMatchObject({ type: "output", data: { output: "subagent says hi", parent_tool_use_id: "tool:x" } });
  });

  it("on resync_required, refetches the snapshot and performs a fresh onSnapshot replace (not an upsert)", async () => {
    let call = 0;
    fetchMock.mockImplementation(() => {
      call += 1;
      return jsonResponse(
        call === 1
          ? snapshotBody("inc-1", 0, "turn-a", 100)
          : snapshotBody("inc-2", 0, "turn-a", 100), // fresh incarnation after resync
      );
    });
    const onSnapshot = vi.fn();
    renderHook(() => useSurfaceSession({ sessionId: SESSION, onSnapshot, onUpsertMessage: vi.fn() }));
    await waitFor(() => expect(onSnapshot).toHaveBeenCalledTimes(1));

    act(() => {
      ws.emit({ type: "resync_required", cv: 1, surface_id: SESSION } as unknown as import("../src/types").WSEvent);
    });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(onSnapshot).toHaveBeenCalledTimes(2));
    const [, secondBatch] = onSnapshot.mock.calls[1] as [string, ChatMessage[]];
    expect(secondBatch.length).toBeGreaterThan(0);
  });

  it("closes the socket and stops issuing updates when the session changes away", async () => {
    fetchMock.mockImplementation(() => jsonResponse(snapshotBody("inc-1", 0, "turn-a", 100)));
    const onSnapshot = vi.fn();
    const { rerender } = renderHook(
      ({ sessionId }: { sessionId: string | null }) =>
        useSurfaceSession({ sessionId, onSnapshot, onUpsertMessage: vi.fn() }),
      { initialProps: { sessionId: SESSION as string | null } },
    );
    await waitFor(() => expect(onSnapshot).toHaveBeenCalledTimes(1));
    const socketBeforeSwitch = ws.getCurrent();

    rerender({ sessionId: null });
    expect(socketBeforeSwitch.readyState).toBe(3 /* CLOSED */);
  });
});
