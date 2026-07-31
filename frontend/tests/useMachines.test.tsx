import { describe, beforeEach, afterEach, it, expect, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import type { NodeSnapshot, NodeStateChangedData } from "../src/types";

// useMachines keeps its topology snapshot in module-level singleton state
// and attaches a window listener at module load. Every test resets the
// module registry and re-imports for an isolated store + listener set,
// mirroring the usePendingNodeRegistrations precedent.
type MachinesModule = typeof import("../src/hooks/useMachines");

async function fresh(): Promise<MachinesModule> {
  vi.resetModules();
  return await import("../src/hooks/useMachines");
}

function jsonResp(body: unknown, ok = true, status = ok ? 200 : 400): Response {
  return { ok, status, json: async () => body } as unknown as Response;
}

function node(id = "n1", over: Partial<NodeSnapshot> = {}): NodeSnapshot {
  return {
    id,
    role: "worker_node",
    address: "1.2.3.4:1",
    cwd_roots: ["/repo"],
    state: "connected",
    connected_at: 1000,
    last_seen: 2000,
    app_commit_sha: "app-sha",
    app_dirty: false,
    primary_commit_sha: "pri-sha",
    primary_dirty: false,
    version_status: "ok",
    ...over,
  };
}

function dispatchNodeStateChanged(detail: unknown): void {
  window.dispatchEvent(new CustomEvent("node_state_changed", { detail }));
}

// State-mutating handlers end in _notify() → React force update, so the
// dispatch must flush inside act() before result.current is read.
async function fire(fn: () => void): Promise<void> {
  await act(async () => {
    fn();
  });
}

// Find the fetch call whose URL contains `needle`, optionally filtered by method.
// A fetch with no explicit method is a GET (opts.method is undefined).
function callFor(
  fetchMock: ReturnType<typeof vi.fn>,
  needle: string,
  method?: string,
): [unknown, RequestInit | undefined] | undefined {
  return fetchMock.mock.calls.find((c) => {
    const url = String(c[0]);
    if (!url.includes(needle)) return false;
    const m = (c[1] as RequestInit | undefined)?.method ?? "GET";
    if (method && m !== method) return false;
    return true;
  }) as [unknown, RequestInit | undefined] | undefined;
}

describe("useMachines", () => {
  let originalFetch: typeof globalThis.fetch;
  // fresh() re-imports the module, which re-attaches a node_state_changed
  // window listener. Without removal these accumulate across tests and stale
  // modules answer every dispatch. Track every listener added during a test
  // and tear them down in afterEach.
  let registered: Array<{ type: string; cb: EventListenerOrEventListenerObject }>;
  let origAddEventListener: typeof window.addEventListener;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResp([])) as unknown as typeof globalThis.fetch;
    registered = [];
    origAddEventListener = window.addEventListener.bind(window);
    vi.spyOn(window, "addEventListener").mockImplementation(((type: string, cb: EventListenerOrEventListenerObject) => {
      registered.push({ type, cb });
      return origAddEventListener(type, cb);
    }) as typeof window.addEventListener);
  });

  afterEach(() => {
    for (const { type, cb } of registered) window.removeEventListener(type, cb);
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  // --- _refetch / useMachines mount path ---

  it("refetches the node snapshot on the first authed mount", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResp([node("n1"), node("n2")]));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const { result } = renderHook(() => mod.useMachines("authed"));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.machines.map((m) => m.id)).toEqual(["n1", "n2"]);
    const get = callFor(fetchMock, "/nodes", "GET");
    expect(get).toBeTruthy();
    expect((get![1] as RequestInit).credentials).toBe("include");
  });

  it("treats a non-ok response as an empty machine list", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResp({}, false, 401)) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const { result } = renderHook(() => mod.useMachines("authed"));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.machines).toEqual([]);
  });

  it("ignores a malformed (non-array) snapshot payload", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResp({ not: "an array" })) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const { result } = renderHook(() => mod.useMachines("authed"));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.machines).toEqual([]);
  });

  it("falls back to an empty list when the snapshot fetch rejects", async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("offline")) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const { result } = renderHook(() => mod.useMachines("authed"));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.machines).toEqual([]);
  });

  it("does not fetch while unauthed and leaves loading true", async () => {
    const fetchMock = vi.fn();
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const { result } = renderHook(() => mod.useMachines("guest"));
    await Promise.resolve();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.loading).toBe(true);
    expect(result.current.machines).toEqual([]);
  });

  it("shares one fetch across N mounted consumers", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResp([node()]));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const a = renderHook(() => mod.useMachines("authed"));
    await waitFor(() => expect(a.result.current.loading).toBe(false));
    // Second mount sees loading already false → no second GET.
    const b = renderHook(() => mod.useMachines("authed"));
    expect(b.result.current.machines).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("collapses a node-state-changed refetch onto the in-flight request", async () => {
    let release!: () => void;
    const hang = new Promise<Response>((resolve) => (release = () => resolve(jsonResp([node()]))));
    const fetchMock = vi.fn().mockReturnValue(hang);
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    renderHook(() => mod.useMachines("authed"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // Unknown node appears live while the first GET is still pending —
    // _onNodeStateChanged routes through _refetch, which must NOT start a
    // second request (the in-flight promise is shared).
    await fire(() => dispatchNodeStateChanged({ node_id: "ghost", state: "connected" }));
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      release();
      await hang;
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  // --- _onNodeStateChanged patch path ---

  it("ignores node-state-changed events with no usable payload", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResp([node("n1")]));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const { result } = renderHook(() => mod.useMachines("authed"));
    await waitFor(() => expect(result.current.loading).toBe(false));
    const before = result.current.machines[0];

    await fire(() => dispatchNodeStateChanged(null));
    await fire(() => dispatchNodeStateChanged({ state: "connected" } satisfies Partial<NodeStateChangedData>));
    expect(result.current.machines[0]).toEqual(before);
  });

  it("overwrites every field on a full node-state-changed update", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResp([node("n1")])) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const { result } = renderHook(() => mod.useMachines("authed"));
    await waitFor(() => expect(result.current.loading).toBe(false));

    const full: NodeStateChangedData = {
      node_id: "n1",
      state: "disconnected",
      last_seen: 9999,
      app_commit_sha: "app2",
      app_dirty: true,
      primary_commit_sha: "pri2",
      primary_dirty: true,
      version_status: "mismatch",
    };
    await fire(() => dispatchNodeStateChanged(full));

    const m = result.current.machines[0];
    expect(m.state).toBe("disconnected");
    expect(m.last_seen).toBe(9999);
    expect(m.app_commit_sha).toBe("app2");
    expect(m.app_dirty).toBe(true);
    expect(m.primary_commit_sha).toBe("pri2");
    expect(m.primary_dirty).toBe(true);
    expect(m.version_status).toBe("mismatch");
  });

  it("preserves prior fields (incl. last_seen) on a partial update", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResp([node("n1", { last_seen: 2000, app_commit_sha: "app-sha", version_status: "ok" })])) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const { result } = renderHook(() => mod.useMachines("authed"));
    await waitFor(() => expect(result.current.loading).toBe(false));

    // Partial: only state + node_id, last_seen omitted, version_status omitted.
    await fire(() => dispatchNodeStateChanged({ node_id: "n1", state: "unknown" }));
    let m = result.current.machines[0];
    expect(m.state).toBe("unknown");
    expect(m.last_seen).toBe(2000);
    expect(m.app_commit_sha).toBe("app-sha");
    expect(m.version_status).toBe("ok");

    // A `disconnected` transition carries last_seen: null — prior heartbeat
    // must be PRESERVED (null ?? prior === prior), not erased.
    await fire(() => dispatchNodeStateChanged({ node_id: "n1", state: "disconnected", last_seen: null }));
    m = result.current.machines[0];
    expect(m.state).toBe("disconnected");
    expect(m.last_seen).toBe(2000);
  });

  // --- deleteNode ---

  it("deletes a node, refetches, and returns true on success", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResp([node("n1")]));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const ok = await mod.deleteNode("n1");

    expect(ok).toBe(true);
    const del = callFor(fetchMock, "/nodes/n1", "DELETE");
    expect(del).toBeTruthy();
    // Successful delete triggers a snapshot refetch.
    const get = callFor(fetchMock, "/nodes", "GET");
    expect(get).toBeTruthy();
  });

  it("returns false when the delete response is not ok", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResp({}, false, 404)) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const ok = await mod.deleteNode("ghost");
    expect(ok).toBe(false);
  });

  it("returns false when the delete fetch rejects", async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("net")) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const ok = await mod.deleteNode("n1");
    expect(ok).toBe(false);
  });

  // --- restartNode ---

  it("restarts a node and returns true on success", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResp({}));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const ok = await mod.restartNode("n1");

    expect(ok).toBe(true);
    const call = callFor(fetchMock, "/nodes/n1/restart", "POST");
    expect(call).toBeTruthy();
  });

  it("returns false when restart is not ok", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResp({}, false, 500)) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    expect(await mod.restartNode("n1")).toBe(false);
  });

  it("returns false when restart rejects", async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("net")) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    expect(await mod.restartNode("n1")).toBe(false);
  });

  // --- syncProvidersToNode ---

  it("syncs providers with secrets, trimming/filtering provider ids", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResp({ ok: true }));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const res = await mod.syncProvidersToNode("n1", {
      includeSecrets: true,
      providerIds: ["  claude  ", "", "codex", "   "],
    });

    expect(res.ok).toBe(true);
    const call = callFor(fetchMock, "/nodes/n1/sync-providers", "POST");
    expect(call).toBeTruthy();
    const body = JSON.parse((call![1] as RequestInit).body as string);
    expect(body).toEqual({ include_secrets: true, provider_ids: ["claude", "codex"] });
    expect((call![1] as RequestInit).headers).toEqual({ "Content-Type": "application/json" });
  });

  it("syncs providers without secrets (no body) and derives ok from response.ok", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResp({ ok: true }));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const res = await mod.syncProvidersToNode("n1", {});

    expect(res.ok).toBe(true);
    const call = callFor(fetchMock, "/nodes/n1/sync-providers", "POST");
    expect((call![1] as RequestInit).body).toBeUndefined();
    expect((call![1] as RequestInit).headers).toBeUndefined();
  });

  // --- postMachineSync ok-derivation branches ---

  it("derives ok from results.every when no top-level ok is present", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResp({ results: [{ node_id: "n1", ok: true }, { node_id: "n2", ok: true }] }),
    ) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const res = await mod.syncProvidersToNode("n1", { includeSecrets: true });
    expect(res.ok).toBe(true);
    expect(res.results).toHaveLength(2);
  });

  it("fails when any per-node result is not ok", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResp({ results: [{ node_id: "n1", ok: true }, { node_id: "n2", ok: false }] }),
    ) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const res = await mod.syncProvidersToNode("n1", { includeSecrets: true });
    expect(res.ok).toBe(false);
  });

  it("surfaces a string detail/error message and omits non-string ones", async () => {
    // string `detail` → error populated.
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResp({ ok: false, detail: "fingerprint mismatch" }, false, 409),
    ) as unknown as typeof globalThis.fetch;
    const mod = await fresh();
    const res = await mod.syncProvidersToNode("n1", { includeSecrets: true });
    expect(res.ok).toBe(false);
    expect(res.error).toBe("fingerprint mismatch");

    // string `error` → error populated.
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResp({ error: "boom" }),
    ) as unknown as typeof globalThis.fetch;
    const res2 = await mod.syncProvidersToNode("n1", { includeSecrets: true });
    expect(res2.error).toBe("boom");

    // non-string message → error undefined.
    globalThis.fetch = vi.fn().mockResolvedValue(
      jsonResp({ detail: 42 }),
    ) as unknown as typeof globalThis.fetch;
    const res3 = await mod.syncProvidersToNode("n1", { includeSecrets: true });
    expect(res3.error).toBeUndefined();
  });

  it("returns {ok: r.ok} for a non-object response body", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResp("plain string")) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const res = await mod.syncProvidersToNode("n1", { includeSecrets: true });
    expect(res.ok).toBe(true);
    expect(res.results).toBeUndefined();
  });

  it("returns {ok: r.ok} when the response body is unreadable as JSON", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => {
        throw new Error("not json");
      },
    } as unknown as Response) as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const res = await mod.syncProvidersToNode("n1", { includeSecrets: true });
    expect(res.ok).toBe(true);
    expect(res.results).toBeUndefined();
  });

  it("returns ok:false + message when the sync fetch rejects (Error and non-Error)", async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("network down")) as unknown as typeof globalThis.fetch;
    const mod = await fresh();
    const res = await mod.syncProvidersToNode("n1", { includeSecrets: true });
    expect(res.ok).toBe(false);
    expect(res.error).toBe("network down");

    globalThis.fetch = vi.fn().mockRejectedValue("string error") as unknown as typeof globalThis.fetch;
    const res2 = await mod.syncProvidersToNode("n1", { includeSecrets: true });
    expect(res2.ok).toBe(false);
    expect(res2.error).toBe("string error");
  });

  // --- broadcast sync endpoints ---

  it("syncs providers to every connected node", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResp({ ok: true }));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const res = await mod.syncProvidersToConnectedNodes();
    expect(res.ok).toBe(true);
    expect(callFor(fetchMock, "/nodes/sync-providers", "POST")).toBeTruthy();
    // Broadcast path: no per-node id segment, no body.
    const call = callFor(fetchMock, "/nodes/sync-providers", "POST")!;
    expect((call[1] as RequestInit).body).toBeUndefined();
  });

  it("syncs extensions to one node and to every connected node", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResp({ ok: true }));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    expect((await mod.syncExtensionsToNode("n9")).ok).toBe(true);
    expect((await mod.syncExtensionsToConnectedNodes()).ok).toBe(true);

    expect(callFor(fetchMock, "/nodes/n9/sync-extensions", "POST")).toBeTruthy();
    expect(callFor(fetchMock, "/nodes/sync-extensions", "POST")).toBeTruthy();
  });

  it("unmounts without leaking the subscriber", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResp([node("n1")]));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    const mod = await fresh();
    const { result, unmount } = renderHook(() => mod.useMachines("authed"));
    await waitFor(() => expect(result.current.loading).toBe(false));

    unmount();
    // Cleanup deletes the force-update subscriber; a subsequent patch still
    // mutates singleton state safely (no throw) for any later consumer.
    await fire(() => dispatchNodeStateChanged({ node_id: "n1", state: "disconnected", last_seen: null }));
  });
});
