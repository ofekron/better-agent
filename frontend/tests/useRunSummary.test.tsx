// Coverage for `src/lib/runSummaryRegistry.ts` + `src/hooks/useRunSummary.ts`
// (ADR 0009, Package E frontend cutover) — the singleton "one `/ws/v2/surface`
// connection subscribed to the `runs` feed, shared by every RunBadge" store.
//
// `runSummaryRegistry` is a true module-level singleton (constructed once at
// import time), so every test re-imports it fresh via `vi.resetModules()` —
// same isolation pattern `tests/usePricing.test.tsx` already uses for its own
// external-store singleton.

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MockWebSocketController } from "./harness/mockWebSocket";
import type { RunSummaryWire } from "../src/adapter/wire";

function jsonResponse(body: unknown, ok = true, status = 200) {
  return Promise.resolve({
    ok,
    status,
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body),
  } as Response);
}

function runsEnvelope(runs: RunSummaryWire[]) {
  return {
    kind: "ok",
    runs,
    next_cursor: null,
    snapshot_identity: { incarnation: "i1", render_rev: 1, hist_rev: 1 },
  };
}

function runSummary(overrides: Partial<RunSummaryWire> = {}): RunSummaryWire {
  return {
    run_id: "run-1",
    session_id: "s1",
    turn_id: "t1",
    provider_id: "claude",
    runner: "native",
    phase: "running",
    started_at: 1_700_000_000,
    last_heartbeat_at: 1_700_000_005,
    startup: null,
    kind: null,
    ...overrides,
  };
}

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
  vi.restoreAllMocks();
  vi.resetModules();
});

describe("useRunSummary — cold hydrate", () => {
  it("hydrates via GET /runs?session_id= and resolves the matching run", async () => {
    vi.resetModules();
    const { useRunSummary } = await import("../src/hooks/useRunSummary");
    fetchMock.mockResolvedValue(jsonResponse(runsEnvelope([runSummary()])));

    const { result } = renderHook(() => useRunSummary("s1", "run-1"));
    await waitFor(() => expect(result.current?.run_id).toBe("run-1"));
    expect(result.current?.phase).toBe("running");

    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("/api/v2/surface/runs");
    expect(url).toContain("session_id=s1");
  });

  it("stays undefined for a run_id the session's v2 record set doesn't contain", async () => {
    vi.resetModules();
    const { useRunSummary } = await import("../src/hooks/useRunSummary");
    fetchMock.mockResolvedValue(jsonResponse(runsEnvelope([runSummary({ run_id: "other-run" })])));

    const { result } = renderHook(() => useRunSummary("s1", "run-1"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    // Give the resolved hydrate promise's .then a tick to apply.
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current).toBeUndefined();
  });

  it("returns undefined without fetching when sessionId is absent", async () => {
    vi.resetModules();
    const { useRunSummary } = await import("../src/hooks/useRunSummary");
    const { result } = renderHook(() => useRunSummary(undefined, "run-1"));
    expect(result.current).toBeUndefined();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("two hooks for the same session share one hydrate call", async () => {
    vi.resetModules();
    const { useRunSummary } = await import("../src/hooks/useRunSummary");
    fetchMock.mockResolvedValue(
      jsonResponse(runsEnvelope([runSummary({ run_id: "a" }), runSummary({ run_id: "b" })])),
    );

    const { result: ra } = renderHook(() => useRunSummary("s1", "a"));
    const { result: rb } = renderHook(() => useRunSummary("s1", "b"));
    await waitFor(() => expect(ra.current?.run_id).toBe("a"));
    await waitFor(() => expect(rb.current?.run_id).toBe("b"));

    const hydrateCalls = fetchMock.mock.calls.filter(([u]) =>
      String(u).includes("/api/v2/surface/runs"),
    );
    expect(hydrateCalls.length).toBe(1);
  });

  it("a hydrate failure leaves the hook undefined instead of throwing", async () => {
    vi.resetModules();
    const { useRunSummary } = await import("../src/hooks/useRunSummary");
    fetchMock.mockResolvedValue(jsonResponse({}, false, 500));

    const { result } = renderHook(() => useRunSummary("s1", "run-1"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current).toBeUndefined();
  });
});

describe("useRunSummary — live `runs` feed", () => {
  it("subscribes to the runs feed and applies a live run_summary_upsert", async () => {
    vi.resetModules();
    const { useRunSummary } = await import("../src/hooks/useRunSummary");
    fetchMock.mockResolvedValue(jsonResponse(runsEnvelope([])));

    const { result } = renderHook(() => useRunSummary("s1", "run-1"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    // The registry's connection sends `{"feeds": ["runs"]}` once OPEN.
    await waitFor(() => {
      expect(
        ws.outbound.some((f) => Array.isArray((f as { feeds?: unknown[] }).feeds)
          && (f as { feeds: unknown[] }).feeds.includes("runs")),
      ).toBe(true);
    });

    act(() => {
      ws.emit({
        type: "run_summary_upsert",
        cv: 1,
        summary: runSummary({ phase: "stalled", last_heartbeat_at: 1_700_000_100 }),
      } as never);
    });

    await waitFor(() => expect(result.current?.phase).toBe("stalled"));
    expect(result.current?.last_heartbeat_at).toBe(1_700_000_100);
  });

  it("does not apply an upsert for a different session to an unrelated hook", async () => {
    vi.resetModules();
    const { useRunSummary } = await import("../src/hooks/useRunSummary");
    fetchMock.mockResolvedValue(jsonResponse(runsEnvelope([])));

    const { result } = renderHook(() => useRunSummary("s1", "run-1"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await waitFor(() => {
      expect(ws.outbound.some((f) => Array.isArray((f as { feeds?: unknown[] }).feeds))).toBe(true);
    });

    act(() => {
      ws.emit({
        type: "run_summary_upsert",
        cv: 1,
        summary: runSummary({ run_id: "run-1", session_id: "other-session", phase: "stalled" }),
      } as never);
    });

    // Different (session_id, run_id) identity — the registry keys upserts
    // by session_id first, so this must not leak onto session "s1"'s view
    // of "run-1" even though the run_id string collides.
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current?.phase).not.toBe("stalled");
  });

  it("unsubscribing (unmount) stops forcing re-renders on further upserts", async () => {
    vi.resetModules();
    const { useRunSummary } = await import("../src/hooks/useRunSummary");
    fetchMock.mockResolvedValue(jsonResponse(runsEnvelope([runSummary()])));

    const { result, unmount } = renderHook(() => useRunSummary("s1", "run-1"));
    await waitFor(() => expect(result.current?.phase).toBe("running"));
    unmount();

    // No assertion crash / dangling update warning is the pass condition —
    // a further upsert after unmount must not throw (React would raise on
    // a setState call against an unmounted hook if the unsubscribe leaked).
    expect(() => {
      act(() => {
        ws.emit({
          type: "run_summary_upsert",
          cv: 2,
          summary: runSummary({ phase: "completed" }),
        } as never);
      });
    }).not.toThrow();
  });
});

// B1/B3 parity restoration: leaf/RunningIndicator.tsx's kind label needs a
// run-summary lookup keyed by `turn_id` (the one correlation key a content
// node has — `NodeWire.run_ref` is never populated by the current backend
// adapter), not `run_id` — same shared registry, a second lookup index.
describe("useRunSummaryByTurn — lookup by turn_id", () => {
  it("resolves the run whose turn_id matches, carrying its kind", async () => {
    vi.resetModules();
    const { useRunSummaryByTurn } = await import("../src/hooks/useRunSummary");
    fetchMock.mockResolvedValue(
      jsonResponse(runsEnvelope([runSummary({ turn_id: "t1", kind: "manager" })])),
    );

    const { result } = renderHook(() => useRunSummaryByTurn("s1", "t1"));
    await waitFor(() => expect(result.current?.kind).toBe("manager"));
    expect(result.current?.run_id).toBe("run-1");
  });

  it("stays undefined for a turn_id no known run carries", async () => {
    vi.resetModules();
    const { useRunSummaryByTurn } = await import("../src/hooks/useRunSummary");
    fetchMock.mockResolvedValue(jsonResponse(runsEnvelope([runSummary({ turn_id: "other-turn" })])));

    const { result } = renderHook(() => useRunSummaryByTurn("s1", "t1"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current).toBeUndefined();
  });

  it("returns undefined without fetching when sessionId or turnId is absent", async () => {
    vi.resetModules();
    const { useRunSummaryByTurn } = await import("../src/hooks/useRunSummary");
    const { result } = renderHook(() => useRunSummaryByTurn(undefined, "t1"));
    expect(result.current).toBeUndefined();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("picks up a live run_summary_upsert's kind for the matching turn_id", async () => {
    vi.resetModules();
    const { useRunSummaryByTurn } = await import("../src/hooks/useRunSummary");
    fetchMock.mockResolvedValue(jsonResponse(runsEnvelope([])));

    const { result } = renderHook(() => useRunSummaryByTurn("s1", "t1"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await waitFor(() => {
      expect(ws.outbound.some((f) => Array.isArray((f as { feeds?: unknown[] }).feeds))).toBe(true);
    });

    act(() => {
      ws.emit({
        type: "run_summary_upsert",
        cv: 1,
        summary: runSummary({ turn_id: "t1", kind: "worker" }),
      } as never);
    });

    await waitFor(() => expect(result.current?.kind).toBe("worker"));
  });
});
