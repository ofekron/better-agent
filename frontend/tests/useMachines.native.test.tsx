import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Dedicated coverage for useMachines.ts's `syncProvidersToNode`'s NATIVE
// (`sync_node_providers` intent) path — `useMachines.test.tsx` exercises
// the legacy REST fallback exclusively (the broad suite's global
// `lib/systemFeedRegistry` stub always returns `null`/never fires, per
// `tests/setup.ts`); this file overrides that stub with a controllable
// fake feed so the event-driven "collect node_provider_credential_upsert
// frames until every requested provider settles" completion path is
// actually exercised.

type Listener = (frame: unknown) => void;

const feed = vi.hoisted(() => ({
  listeners: new Set<Listener>(),
  submitSystemIntent: vi.fn(),
}));

vi.mock("../src/lib/systemFeedRegistry", () => ({
  SYSTEM_FEED_NAMES: [],
  subscribeSystemFrames: (cb: Listener) => {
    feed.listeners.add(cb);
    return () => feed.listeners.delete(cb);
  },
  subscribeSystemSocketConnection: () => () => {},
  isSystemSocketOpen: () => true,
  submitSystemIntent: (...a: unknown[]) => feed.submitSystemIntent(...a),
  waitForSystemFrame: () => new Promise(() => {}),
  submitSystemIntentAwaitingUpsert: () => null,
}));

function emit(frame: unknown): void {
  for (const listener of [...feed.listeners]) listener(frame);
}

function credentialUpsert(nodeId: string, providerId: string, status: string, failureCode?: string) {
  return {
    type: "node_provider_credential_upsert",
    cv: 1,
    status: {
      node_id: nodeId, provider_id: providerId, cv: 1, status,
      authorized_at: null, updated_at: null, failure_code: failureCode ?? null,
    },
  };
}

type MachinesModule = typeof import("../src/hooks/useMachines");

async function fresh(): Promise<MachinesModule> {
  vi.resetModules();
  return await import("../src/hooks/useMachines");
}

describe("useMachines — native syncProvidersToNode", () => {
  let originalFetch: typeof globalThis.fetch;
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
    fetchMock = vi.fn();
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;
    // Reset BEFORE each test's own `mockResolvedValue` configuration —
    // resetting inside `fresh()` instead would run AFTER the test already
    // configured it (every test calls `fresh()` second), clobbering it.
    feed.listeners.clear();
    feed.submitSystemIntent.mockReset();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("submits the credential-sync intent instead of the REST route when includeSecrets + providerIds are given", async () => {
    feed.submitSystemIntent.mockResolvedValue({ type: "intent_accepted", intent_id: "i1" });
    const mod = await fresh();

    const resultPromise = mod.syncProvidersToNode("n1", { includeSecrets: true, providerIds: ["p1"] });
    emit(credentialUpsert("n1", "p1", "synced"));
    const result = await resultPromise;

    expect(feed.submitSystemIntent).toHaveBeenCalledWith({
      kind: "sync_node_providers", node_id: "n1", include_secrets: true, provider_ids: ["p1"],
    });
    // REST fallback never hit.
    expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("sync-providers"))).toBe(false);
    expect(result).toEqual({
      ok: true,
      results: [{ node_id: "n1", provider_id: "p1", ok: true, error: undefined }],
    });
  });

  it("collects multiple providers' terminal statuses (one failed) into one MachineSyncResult", async () => {
    feed.submitSystemIntent.mockResolvedValue({ type: "intent_accepted", intent_id: "i2" });
    const mod = await fresh();

    const resultPromise = mod.syncProvidersToNode("n1", { includeSecrets: true, providerIds: ["p1", "p2"] });
    // Intermediate "pending" status must be ignored, not treated as terminal.
    emit(credentialUpsert("n1", "p1", "pending"));
    emit(credentialUpsert("n1", "p1", "synced"));
    emit(credentialUpsert("n1", "p2", "failed", "rpc_failed"));
    const result = await resultPromise;

    expect(result.ok).toBe(false);
    expect(result.results).toEqual([
      { node_id: "n1", provider_id: "p1", ok: true, error: undefined },
      { node_id: "n1", provider_id: "p2", ok: false, error: "rpc_failed" },
    ]);
  });

  it("ignores a credential upsert for a different node_id", async () => {
    feed.submitSystemIntent.mockResolvedValue({ type: "intent_accepted", intent_id: "i3" });
    const mod = await fresh();

    const resultPromise = mod.syncProvidersToNode("n1", { includeSecrets: true, providerIds: ["p1"] });
    emit(credentialUpsert("other-node", "p1", "synced"));
    emit(credentialUpsert("n1", "p1", "synced"));
    const result = await resultPromise;

    expect(result.results).toEqual([{ node_id: "n1", provider_id: "p1", ok: true, error: undefined }]);
  });

  it("resolves {ok:false, error} on an intent rejection without waiting for any frame", async () => {
    feed.submitSystemIntent.mockResolvedValue({
      type: "intent_rejected", intent_id: "i4", code: "409", message: "node not connected",
    });
    const mod = await fresh();

    const result = await mod.syncProvidersToNode("n1", { includeSecrets: true, providerIds: ["p1"] });
    expect(result).toEqual({ ok: false, error: "node not connected" });
  });

  it("falls back to the legacy REST route when includeSecrets is false", async () => {
    fetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => ({ ok: true }) });
    const mod = await fresh();

    await mod.syncProvidersToNode("n1", {});

    expect(feed.submitSystemIntent).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("sync-providers"))).toBe(true);
  });

  it("falls back to the legacy REST route when providerIds is empty even with includeSecrets", async () => {
    fetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => ({ ok: true }) });
    const mod = await fresh();

    await mod.syncProvidersToNode("n1", { includeSecrets: true, providerIds: [] });

    expect(feed.submitSystemIntent).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.some((c) => String(c[0]).includes("sync-providers"))).toBe(true);
  });
});
