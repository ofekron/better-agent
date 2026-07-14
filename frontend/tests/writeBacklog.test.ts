import { describe, beforeEach, afterEach, it, expect, vi } from "vitest";

// writeBacklog persists to + loads from this localStorage key on import, so
// each test resets storage and re-imports the module for a clean queue.
const BACKLOG_KEY = "better-agent-write-backlog";

async function fresh() {
  vi.resetModules();
  return (await import("../src/utils/writeBacklog")) as typeof import("../src/utils/writeBacklog");
}

function res(ok: boolean, status: number) {
  return { ok, status } as Response;
}

describe("writeBacklog", () => {
  let originalFetch: typeof globalThis.fetch;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
    localStorage.clear();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("drops a write after the backend acknowledges it (2xx)", async () => {
    const mod = await fresh();
    const fetchMock = vi.fn().mockResolvedValue(res(true, 200));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    mod.queueWrite({ method: "PATCH", url: "/api/ui-selection", body: { x: 1 }, key: "k" });
    await mod.flushWriteBacklog();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(JSON.parse(localStorage.getItem(BACKLOG_KEY) ?? "[]")).toEqual([]);
  });

  it("retries transient failures (5xx / network error)", async () => {
    const mod = await fresh();
    const fetchMock = vi
      .fn()
      .mockResolvedValue(res(false, 503))
      .mockRejectedValueOnce(new Error("offline"));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    mod.queueWrite({ method: "PUT", url: "/u", body: { a: 1 }, key: "k" });
    await mod.flushWriteBacklog();
    // Still queued after a failed sweep.
    expect(JSON.parse(localStorage.getItem(BACKLOG_KEY) ?? "[]")).toHaveLength(1);

    await mod.flushWriteBacklog();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(JSON.parse(localStorage.getItem(BACKLOG_KEY) ?? "[]")).toHaveLength(1);
  });

  it("keeps a rejected write until an explicit backend acknowledgement", async () => {
    const mod = await fresh();
    const fetchMock = vi.fn().mockResolvedValue(res(false, 422));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    mod.queueWrite({ method: "PATCH", url: "/u", body: {}, key: "k" });
    await mod.flushWriteBacklog();
    await mod.flushWriteBacklog();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(JSON.parse(localStorage.getItem(BACKLOG_KEY) ?? "[]")).toHaveLength(1);
  });

  it("collapses same-key writes to the latest (take_latest)", async () => {
    const mod = await fresh();
    const fetchMock = vi.fn().mockResolvedValue(res(true, 200));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    mod.queueWrite({ method: "PATCH", url: "/u", body: { v: 1 }, key: "same" });
    mod.queueWrite({ method: "PATCH", url: "/u", body: { v: 2 }, key: "same" });
    mod.queueWrite({ method: "PATCH", url: "/u", body: { v: 3 }, key: "same" });
    await mod.flushWriteBacklog();

    // The first fetch carries v:1 (snapshotted before the collapse); the
    // re-sweep carries v:3. v:2 is never sent (collapsed away).
    const bodies = fetchMock.mock.calls.map(
      (c) => JSON.parse((c[1] as RequestInit).body as string) as { v?: number },
    );
    expect(bodies).toContainEqual({ v: 1 });
    expect(bodies).toContainEqual({ v: 3 });
    expect(bodies).not.toContainEqual({ v: 2 });
    expect(JSON.parse(localStorage.getItem(BACKLOG_KEY) ?? "[]")).toEqual([]);
  });

  it("keeps independent keys separate", async () => {
    const mod = await fresh();
    const fetchMock = vi.fn().mockResolvedValue(res(true, 200));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;

    mod.queueWrite({ method: "PATCH", url: "/u", body: { a: 1 }, key: "k1" });
    mod.queueWrite({ method: "PATCH", url: "/u", body: { b: 2 }, key: "k2" });
    await mod.flushWriteBacklog();

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("survives a reload: a persisted backlog drains on the next flush", async () => {
    const mod = await fresh();
    const fail = vi.fn().mockResolvedValue(res(false, 503));
    globalThis.fetch = fail as unknown as typeof globalThis.fetch;

    mod.queueWrite({ method: "PATCH", url: "/u", body: { x: 9 }, key: "k" });
    await mod.flushWriteBacklog(); // stays queued (503), persisted to localStorage

    // Simulate a page reload: fresh module reads the persisted backlog.
    const reloaded = await fresh();
    const ok = vi.fn().mockResolvedValue(res(true, 200));
    globalThis.fetch = ok as unknown as typeof globalThis.fetch;
    await reloaded.flushWriteBacklog();

    expect(ok).toHaveBeenCalledTimes(1);
    const sent = JSON.parse((ok.mock.calls[0][1] as RequestInit).body as string);
    expect(sent).toEqual({ x: 9 });
    expect(JSON.parse(localStorage.getItem(BACKLOG_KEY) ?? "[]")).toEqual([]);
  });

  it("reuses a stable idempotency identity across reconnect and reload", async () => {
    const mod = await fresh();
    const offline = vi.fn().mockResolvedValue(res(false, 503));
    globalThis.fetch = offline as unknown as typeof globalThis.fetch;
    mod.queueWrite({ method: "PATCH", url: "/api/ui-selection", body: { x: 1 }, key: "selection" });
    await mod.flushWriteBacklog();
    const firstId = (offline.mock.calls[0][1] as RequestInit).headers as Record<string, string>;

    const reloaded = await fresh();
    const online = vi.fn().mockResolvedValue(res(true, 200));
    globalThis.fetch = online as unknown as typeof globalThis.fetch;
    reloaded.signalReconnect();
    await reloaded.flushWriteBacklog();
    const replayId = (online.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(replayId["X-Idempotency-Key"]).toBe(firstId["X-Idempotency-Key"]);
  });

  it("preserves order and only clears the acknowledged prefix on partial sync", async () => {
    const mod = await fresh();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(res(true, 200))
      .mockResolvedValueOnce(res(false, 503));
    globalThis.fetch = fetchMock as unknown as typeof globalThis.fetch;
    mod.queueWrite({ method: "PATCH", url: "/u", body: { order: 1 }, key: "one" });
    mod.queueWrite({ method: "PATCH", url: "/u", body: { order: 2 }, key: "two" });
    await mod.flushWriteBacklog();
    expect(fetchMock.mock.calls.map((call) => JSON.parse((call[1] as RequestInit).body as string).order)).toEqual([1, 2]);
    const stored = JSON.parse(localStorage.getItem(BACKLOG_KEY) ?? "[]") as Array<{ body: { order: number } }>;
    expect(stored.map((write) => write.body.order)).toEqual([2]);
  });
});
