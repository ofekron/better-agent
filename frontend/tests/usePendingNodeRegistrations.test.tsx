import { describe, beforeEach, afterEach, it, expect, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import type { NodeRegistrationResolvedData, PendingNodeRegistration } from "../src/types";

// The hook keeps its pending snapshot in module-level singleton state
// (mirrors useMachines / writeBacklog), so every test resets the module
// registry and re-imports for an isolated store + listener set.
type HookModule = typeof import("../src/hooks/usePendingNodeRegistrations");

async function fresh(): Promise<HookModule> {
  vi.resetModules();
  return await import("../src/hooks/usePendingNodeRegistrations");
}

function jsonResp(body: unknown, ok = true, status = ok ? 200 : 400): Response {
  return { ok, status, json: async () => body } as unknown as Response;
}

const EMPTY = jsonResp({ pending_nodes: [] });

function rec(node_id = "n1"): PendingNodeRegistration {
  return {
    node_id,
    address: "1.2.3.4:1",
    cwd_roots: ["/repo"],
    fingerprint: "fp-" + node_id,
    status: "pending",
    created_at: "2026-01-01T00:00:00Z",
    expires_at: "2026-01-01T01:00:00Z",
  };
}

async function publishRequested(detail: unknown): Promise<void> {
  const { eventBus } = await import("../src/lib/eventBus");
  eventBus.publish("node_registration_requested", detail);
}
async function publishResolved(detail: unknown): Promise<void> {
  const { eventBus } = await import("../src/lib/eventBus");
  eventBus.publish("node_registration_resolved", detail);
}

// Every state-mutating event handler ends in _notify() → React force update,
// so dispatches must run inside act() for the new render to flush before we
// read result.current.
async function fire(fn: () => void | Promise<void>): Promise<void> {
  await act(async () => {
    await fn();
  });
}

describe("usePendingNodeRegistrations", () => {
  let originalFetch: typeof globalThis.fetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
    // Default: an authed GET returns an empty pending list. Tests override
    // globalThis.fetch when they need a different response.
    globalThis.fetch = vi.fn().mockResolvedValue(EMPTY) as unknown as typeof globalThis.fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("refetches the pending snapshot on the first authed mount", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResp({ pending_nodes: [rec()] }));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const { result } = renderHook(() => mod.usePendingNodeRegistrations("authed"));
    await waitFor(() => expect(result.current).toHaveLength(1));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/pending_nodes");
    expect(result.current[0].node_id).toBe("n1");
  });

  it("does not fetch while unauthed", async () => {
    const fetchMock = vi.fn();
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    renderHook(() => mod.usePendingNodeRegistrations("guest"));
    await Promise.resolve();

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("treats a non-ok response as an empty pending list", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResp({}, false, 401)) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const { result } = renderHook(() => mod.usePendingNodeRegistrations("authed"));
    await waitFor(() => expect(result.current).toEqual([]));
  });

  it("ignores a malformed pending_nodes payload", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResp({ pending_nodes: "nope" })) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const { result } = renderHook(() => mod.usePendingNodeRegistrations("authed"));
    await waitFor(() => expect(result.current).toEqual([]));
  });

  it("preserves prior state when a refetch fails transiently", async () => {
    // Seed via a requested event, then make the reconnect refetch throw —
    // the catch path must leave the seeded row intact.
    const mod = await fresh();
    const { eventBus } = await import("../src/lib/eventBus");
    const { result } = renderHook(() => mod.usePendingNodeRegistrations("authed"));
    // Let the mount-time GET (EMPTY) settle before seeding so it can't
    // clobber the dispatched row.
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
    await act(async () => {
      await Promise.resolve();
    });
    await fire(() => publishRequested(rec("seed")));
    expect(result.current).toHaveLength(1);

    globalThis.fetch = vi.fn().mockRejectedValue(new Error("offline")) as unknown as typeof globalThis.fetch;
    await act(async () => {
      eventBus.publish("ws_connection_changed", { connected: true });
    });
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());

    expect(result.current).toHaveLength(1);
    expect(result.current[0].node_id).toBe("seed");
  });

  it("collapses concurrent refetch triggers onto one in-flight request", async () => {
    let release!: () => void;
    const hang = new Promise<Response>((resolve) => (release = () => resolve(EMPTY)));
    const fetchMock = vi.fn().mockReturnValue(hang);
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const { eventBus } = await import("../src/lib/eventBus");
    renderHook(() => mod.usePendingNodeRegistrations("authed"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    // Socket reconnects while the first GET is still pending — must NOT
    // start a second request (the in-flight promise is shared).
    await act(async () => {
      eventBus.publish("ws_connection_changed", { connected: true });
    });
    await act(async () => {
      release();
      await hang;
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("reconnect only refetches when the client is authed", async () => {
    const fetchMock = vi.fn();
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    await fresh();
    const { eventBus } = await import("../src/lib/eventBus");
    // Never mount the hook → _authed stays false → reconnect is a no-op.
    eventBus.publish("ws_connection_changed", { connected: true });
    eventBus.publish("ws_connection_changed", { connected: false });
    await Promise.resolve();

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("upserts a freshly requested node and replaces a re-dial by node_id", async () => {
    const mod = await fresh();
    // Guest mount: exercise the eventBus handlers without a GET that
    // would reset the seeded state when it resolves.
    const { result } = renderHook(() => mod.usePendingNodeRegistrations("guest"));

    await fire(() => publishRequested(rec("n1")));
    expect(result.current).toHaveLength(1);
    expect(result.current[0].fingerprint).toBe("fp-n1");

    // A second distinct node appends.
    await fire(() => publishRequested(rec("n2")));
    expect(result.current).toHaveLength(2);

    // Re-dial of n1 with a new fingerprint REPLACES the stale row, not adds.
    const reDial = rec("n1");
    reDial.fingerprint = "fp-n1-rotated";
    await fire(() => publishRequested(reDial));
    expect(result.current).toHaveLength(2);
    const n1 = result.current.find((p) => p.node_id === "n1");
    expect(n1?.fingerprint).toBe("fp-n1-rotated");
  });

  it("ignores a requested event with no usable payload", async () => {
    const mod = await fresh();
    const { result } = renderHook(() => mod.usePendingNodeRegistrations("guest"));

    await fire(() => publishRequested(null));
    await fire(() => publishRequested({ node_id: "" }));
    expect(result.current).toEqual([]);
  });

  it("drops a node when its registration resolves, and no-ops when absent", async () => {
    const mod = await fresh();
    const { result } = renderHook(() => mod.usePendingNodeRegistrations("guest"));

    await fire(() => publishRequested(rec("n1")));
    await fire(() => publishRequested(rec("n2")));
    expect(result.current).toHaveLength(2);

    const resolved: NodeRegistrationResolvedData = { node_id: "n1", status: "approved" };
    await fire(() => publishResolved(resolved));
    expect(result.current).toHaveLength(1);
    expect(result.current[0].node_id).toBe("n2");

    // Resolving an unknown node is a no-op (no mutation, no notify).
    await fire(() => publishResolved({ node_id: "ghost", status: "denied" }));
    expect(result.current).toHaveLength(1);
    // No payload at all is also a safe no-op.
    await fire(() => publishResolved(null));
    expect(result.current).toHaveLength(1);
  });

  it("shares one fetch across N mounted consumers", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResp({ pending_nodes: [rec()] }));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const a = renderHook(() => mod.usePendingNodeRegistrations("authed"));
    await waitFor(() => expect(a.result.current).toHaveLength(1));
    // A second mount sees _loaded already true → no second GET.
    const b = renderHook(() => mod.usePendingNodeRegistrations("authed"));
    expect(b.result.current).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("approves a node locally and on the backend", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResp({}));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    // Guest mount: no GET refetch to race the seeded state.
    const { result } = renderHook(() => mod.usePendingNodeRegistrations("guest"));
    await fire(() => publishRequested(rec("n1")));
    expect(result.current).toHaveLength(1);

    await act(async () => {
      await mod.approveNodeRegistration("n1");
    });

    const approveCall = fetchMock.mock.calls.find((c) =>
      String(c[0]).includes("/pending_nodes/n1/approve"),
    );
    expect(approveCall).toBeTruthy();
    expect((approveCall![1] as RequestInit).method).toBe("POST");
    // Local optimistic removal — no round-trip wait.
    expect(result.current).toEqual([]);
  });

  it("surfaces the backend detail message when approve fails", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResp({ detail: "fingerprint mismatch" }, false, 409)) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    renderHook(() => mod.usePendingNodeRegistrations("guest"));
    await expect(mod.approveNodeRegistration("n1")).rejects.toThrow("fingerprint mismatch");
  });

  it("falls back to a status message when the approve error body is unreadable", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => {
        throw new Error("not json");
      },
    } as unknown as Response) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    renderHook(() => mod.usePendingNodeRegistrations("guest"));
    await expect(mod.approveNodeRegistration("n1")).rejects.toThrow("approve failed (500)");
  });

  it("denies a node locally and on the backend", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResp({}));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const { result } = renderHook(() => mod.usePendingNodeRegistrations("guest"));
    await fire(() => publishRequested(rec("n1")));
    await act(async () => {
      await mod.denyNodeRegistration("n1");
    });

    const denyCall = fetchMock.mock.calls.find((c) =>
      String(c[0]).includes("/pending_nodes/n1/deny"),
    );
    expect(denyCall).toBeTruthy();
    expect((denyCall![1] as RequestInit).method).toBe("POST");
    expect(result.current).toEqual([]);
  });

  it("throws when deny fails", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResp({ detail: "already resolved" }, false, 409)) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    renderHook(() => mod.usePendingNodeRegistrations("guest"));
    await expect(mod.denyNodeRegistration("n1")).rejects.toThrow("already resolved");
  });

  it("falls back to a status message when the deny error body is unreadable", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => {
        throw new Error("not json");
      },
    } as unknown as Response) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    renderHook(() => mod.usePendingNodeRegistrations("guest"));
    await expect(mod.denyNodeRegistration("n1")).rejects.toThrow("deny failed (503)");
  });

  it("unmounts without leaking the subscriber", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResp({ pending_nodes: [rec()] }));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const { result, unmount } = renderHook(() => mod.usePendingNodeRegistrations("authed"));
    await waitFor(() => expect(result.current).toHaveLength(1));

    // Cleanup runs the delete-from-subscribers path; no throw, and the
    // module-level handlers still update state for any remaining consumer.
    unmount();
    await fire(() => publishRequested(rec("n2")));
    // A fresh mount observes the singleton's advanced state.
    const { result: result2 } = renderHook(() => mod.usePendingNodeRegistrations("authed"));
    expect(result2.current.some((p) => p.node_id === "n2")).toBe(true);
  });
});
