// Coverage for `src/lib/sessionSurfaceRegistry.ts` (ADR 0008, Package B) —
// the shared "sessions" feed connection singleton. Same isolation pattern
// `tests/useRunSummary.test.tsx`/`tests/useProviderSurfaceSocket.test.ts`
// use for their own module-level singletons: every test re-imports the
// module fresh via `vi.resetModules()`.

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MockWebSocketController } from "./harness/mockWebSocket";
import type { FolderUpsertFrame, ProjectWire, SessionsFrame, SessionSummaryWire } from "../src/adapter/wire";

// `tests/setup.ts` globally stubs this module for the broad suite (see that
// file's comment) — this file un-mocks it to exercise the REAL
// implementation, same escape hatch `tests/interactionResolveSocket.test.tsx`
// uses for its own always-on singleton.
vi.unmock("../src/lib/sessionSurfaceRegistry");

function jsonResponse(body: unknown, ok = true, status = 200) {
  return Promise.resolve({
    ok,
    status,
    text: () => Promise.resolve(JSON.stringify(body)),
    json: () => Promise.resolve(body),
  } as Response);
}

function sessionsEnvelope(sessions: SessionSummaryWire[]) {
  return {
    kind: "ok",
    sessions,
    next_cursor: null,
    snapshot_identity: { incarnation: "i1", render_rev: 1, hist_rev: 1 },
  };
}

function listEnvelope<T>(value: T[]) {
  return { kind: "ok", value, snapshot_identity: { incarnation: "i1", render_rev: 1, hist_rev: 1 } };
}

function sessionSummary(overrides: Partial<SessionSummaryWire> = {}): SessionSummaryWire {
  return {
    session_id: "s1",
    title: "Session 1",
    project_ref: null,
    selectors: {
      provider_id: "claude", runtime_profile_id: null, model: null,
      reasoning_effort: null, orchestration_mode: null, cwd: "/p",
    },
    state: "idle",
    attention_markers: [],
    opened_at: null,
    last_activity_at: 1_700_000_000,
    archived: false,
    folder_ref: null,
    tag_refs: [],
    unread_count: 0,
    has_error: false,
    monitoring_state: "stopped",
    attention_marker_details: [],
    pending_interactions: 0,
    ...overrides,
  };
}

function projectWire(overrides: Partial<ProjectWire> = {}): ProjectWire {
  return { project_ref: "/p", name: "P", order: 0, session_count: 1, ...overrides };
}

function feedFrame(feed: string, outbound: unknown[]): boolean {
  return outbound.some(
    (f) => Array.isArray((f as { feeds?: unknown[] }).feeds)
      && (f as { feeds: unknown[] }).feeds.includes(feed),
  );
}

let ws: MockWebSocketController;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  ws = new MockWebSocketController();
  ws.install();
  fetchMock = vi.fn().mockResolvedValue(jsonResponse(sessionsEnvelope([])));
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  ws.uninstall();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.resetModules();
});

describe("sessionSurfaceRegistry — cold hydrate + live sessions feed", () => {
  it("hydrates via GET /sessions and resolves a session's summary", async () => {
    vi.resetModules();
    const { useSessionSummaryV2 } = await import("../src/lib/sessionSurfaceRegistry");
    fetchMock.mockResolvedValue(jsonResponse(sessionsEnvelope([sessionSummary()])));

    const { result } = renderHook(() => useSessionSummaryV2("s1"));
    await waitFor(() => expect(result.current?.session_id).toBe("s1"));
    expect(result.current?.title).toBe("Session 1");

    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("/api/v2/surface/sessions");
  });

  it("returns undefined without subscribing when sessionId is absent", async () => {
    vi.resetModules();
    const { useSessionSummaryV2 } = await import("../src/lib/sessionSurfaceRegistry");
    const { result } = renderHook(() => useSessionSummaryV2(undefined));
    expect(result.current).toBeUndefined();
  });

  it("applies a live session_summary_upsert over the sessions feed", async () => {
    vi.resetModules();
    const { useSessionSummaryV2 } = await import("../src/lib/sessionSurfaceRegistry");

    const { result } = renderHook(() => useSessionSummaryV2("s1"));
    await waitFor(() => expect(feedFrame("sessions", ws.outbound)).toBe(true));

    act(() => {
      ws.emit({
        type: "session_summary_upsert", cv: 1,
        summary: sessionSummary({ title: "Renamed", unread_count: 3 }),
        intent_id: null,
      } as never);
    });

    await waitFor(() => expect(result.current?.title).toBe("Renamed"));
    expect(result.current?.unread_count).toBe(3);
  });

  it("a session_removed frame clears the entry", async () => {
    vi.resetModules();
    const { useSessionSummaryV2 } = await import("../src/lib/sessionSurfaceRegistry");
    fetchMock.mockResolvedValue(jsonResponse(sessionsEnvelope([sessionSummary()])));

    const { result } = renderHook(() => useSessionSummaryV2("s1"));
    await waitFor(() => expect(result.current?.session_id).toBe("s1"));

    act(() => {
      ws.emit({ type: "session_removed", cv: 2, session_id: "s1" } as never);
    });
    await waitFor(() => expect(result.current).toBeUndefined());
  });

  it("subscribeAllSummaries receives every upsert, not just a subscribed session's own", async () => {
    vi.resetModules();
    const { sessionSurfaceRegistry } = await import("../src/lib/sessionSurfaceRegistry");
    const received: SessionSummaryWire[] = [];
    const unsubscribe = sessionSurfaceRegistry.subscribeAllSummaries((s) => received.push(s));
    await waitFor(() => expect(feedFrame("sessions", ws.outbound)).toBe(true));

    act(() => {
      ws.emit({
        type: "session_summary_upsert", cv: 1, summary: sessionSummary({ session_id: "other" }),
        intent_id: null,
      } as never);
    });

    expect(received).toHaveLength(1);
    expect(received[0].session_id).toBe("other");
    unsubscribe();
  });
});

describe("sessionSurfaceRegistry — B11 model facet (useModelFacetV2)", () => {
  it("computes the distinct model set across all hydrated sessions, scoped by project_ref", async () => {
    vi.resetModules();
    const { useModelFacetV2 } = await import("../src/lib/sessionSurfaceRegistry");
    fetchMock.mockResolvedValue(jsonResponse(sessionsEnvelope([
      sessionSummary({ session_id: "s1", project_ref: "/p", selectors: { provider_id: "claude", runtime_profile_id: null, model: "opus", reasoning_effort: null, orchestration_mode: null, cwd: "/p" } }),
      sessionSummary({ session_id: "s2", project_ref: "/p", selectors: { provider_id: "claude", runtime_profile_id: null, model: "sonnet", reasoning_effort: null, orchestration_mode: null, cwd: "/p" } }),
      sessionSummary({ session_id: "s3", project_ref: "/other", selectors: { provider_id: "claude", runtime_profile_id: null, model: "haiku", reasoning_effort: null, orchestration_mode: null, cwd: "/other" } }),
    ])));

    const { result } = renderHook(() => useModelFacetV2("/p"));
    await waitFor(() => expect(result.current).toEqual(["opus", "sonnet"]));
  });

  it("live session_summary_upsert with a new model updates the facet without a refetch", async () => {
    vi.resetModules();
    const { useModelFacetV2 } = await import("../src/lib/sessionSurfaceRegistry");
    fetchMock.mockResolvedValue(jsonResponse(sessionsEnvelope([
      sessionSummary({ session_id: "s1", project_ref: "/p", selectors: { provider_id: "claude", runtime_profile_id: null, model: "opus", reasoning_effort: null, orchestration_mode: null, cwd: "/p" } }),
    ])));

    const { result } = renderHook(() => useModelFacetV2("/p"));
    await waitFor(() => expect(result.current).toEqual(["opus"]));

    act(() => {
      ws.emit({
        type: "session_summary_upsert", cv: 2,
        summary: sessionSummary({ session_id: "s2", project_ref: "/p", selectors: { provider_id: "claude", runtime_profile_id: null, model: "sonnet", reasoning_effort: null, orchestration_mode: null, cwd: "/p" } }),
        intent_id: null,
      } as never);
    });

    await waitFor(() => expect(result.current).toEqual(["opus", "sonnet"]));
  });

  it("omitting projectRef returns the unfiltered (global) facet", async () => {
    vi.resetModules();
    const { useModelFacetV2 } = await import("../src/lib/sessionSurfaceRegistry");
    fetchMock.mockResolvedValue(jsonResponse(sessionsEnvelope([
      sessionSummary({ session_id: "s1", project_ref: "/p", selectors: { provider_id: "claude", runtime_profile_id: null, model: "opus", reasoning_effort: null, orchestration_mode: null, cwd: "/p" } }),
      sessionSummary({ session_id: "s2", project_ref: "/other", selectors: { provider_id: "claude", runtime_profile_id: null, model: "haiku", reasoning_effort: null, orchestration_mode: null, cwd: "/other" } }),
    ])));

    const { result } = renderHook(() => useModelFacetV2(undefined));
    await waitFor(() => expect(result.current).toEqual(["haiku", "opus"]));
  });
});

describe("sessionSurfaceRegistry — projects", () => {
  it("hydrates via GET /projects and applies a live project_upsert", async () => {
    vi.resetModules();
    const { useProjectsV2 } = await import("../src/lib/sessionSurfaceRegistry");
    fetchMock.mockImplementation((url: string) => {
      if (String(url).includes("/projects")) return jsonResponse(listEnvelope([projectWire()]));
      return jsonResponse(sessionsEnvelope([]));
    });

    const { result } = renderHook(() => useProjectsV2());
    await waitFor(() => expect(result.current).toHaveLength(1));
    expect(result.current[0].project_ref).toBe("/p");

    act(() => {
      ws.emit({
        type: "project_upsert", cv: 2, project: projectWire({ session_count: 5 }), intent_id: null,
      } as never);
    });
    await waitFor(() => expect(result.current[0].session_count).toBe(5));
  });
});

describe("sessionSurfaceRegistry — submitIntent", () => {
  it("returns null (no queueing) when the connection is not open", async () => {
    vi.resetModules();
    const { sessionSurfaceRegistry } = await import("../src/lib/sessionSurfaceRegistry");
    expect(sessionSurfaceRegistry.submitIntent({ kind: "mark_opened", session_id: "s1" })).toBeNull();
  });

  it("stamps cv/intent_id and sends over the shared connection once open", async () => {
    vi.resetModules();
    const { sessionSurfaceRegistry } = await import("../src/lib/sessionSurfaceRegistry");
    // Opening the connection requires a subscriber (ensureOpen is lazy).
    const unsubscribe = sessionSurfaceRegistry.subscribeConnection(() => {});
    await waitFor(() => expect(sessionSurfaceRegistry.isSocketOpen()).toBe(true));

    sessionSurfaceRegistry.submitIntent({ kind: "mark_opened", session_id: "s1" });

    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "mark_opened",
    ) as { intent: Record<string, unknown> } | undefined;
    expect(sent).toBeTruthy();
    expect(sent!.intent.session_id).toBe("s1");
    expect(typeof sent!.intent.intent_id).toBe("string");
    expect(sent!.intent.intent_id).not.toBe("");
    unsubscribe();
  });

  it("resolves as intent_rejected immediately on a synchronous rejection", async () => {
    vi.resetModules();
    const { sessionSurfaceRegistry } = await import("../src/lib/sessionSurfaceRegistry");
    const unsubscribe = sessionSurfaceRegistry.subscribeConnection(() => {});
    await waitFor(() => expect(sessionSurfaceRegistry.isSocketOpen()).toBe(true));

    const ackPromise = sessionSurfaceRegistry.submitIntent({ kind: "mark_opened", session_id: "s1" });
    expect(ackPromise).not.toBeNull();
    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "mark_opened",
    ) as { intent: { intent_id: string } };

    act(() => {
      ws.emit({ type: "intent_rejected", intent_id: sent.intent.intent_id, code: "404", message: "gone" } as never);
    });

    const ack = await ackPromise!;
    expect(ack.type).toBe("intent_rejected");
    unsubscribe();
  });
});

describe("sessionSurfaceRegistry — submitIntentAwaitingRef (event-driven ref correlation)", () => {
  const isFolderUpsert = (f: SessionsFrame): f is FolderUpsertFrame => f.type === "folder_upsert";

  it("resolves { ok: true, frame } once the matching intent_id-stamped FolderUpsert arrives", async () => {
    vi.resetModules();
    const { sessionSurfaceRegistry } = await import("../src/lib/sessionSurfaceRegistry");
    const unsubscribe = sessionSurfaceRegistry.subscribeConnection(() => {});
    await waitFor(() => expect(sessionSurfaceRegistry.isSocketOpen()).toBe(true));

    const resultPromise = sessionSurfaceRegistry.submitIntentAwaitingRef(
      { kind: "create_folder", session_id: null, project_ref: "/p", name: "New", parent_folder_ref: null },
      isFolderUpsert,
    );
    await waitFor(() =>
      expect(ws.outbound.some((f) => (f as { intent?: { kind?: string } }).intent?.kind === "create_folder")).toBe(true));
    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "create_folder",
    ) as { intent: { intent_id: string } };

    act(() => {
      ws.emit({ type: "intent_accepted", intent_id: sent.intent.intent_id } as never);
    });
    act(() => {
      ws.emit({
        type: "folder_upsert", cv: 1,
        folder: { folder_ref: "f-new", project_ref: "/p", name: "New", parent_folder_ref: null, order: 0 },
        intent_id: sent.intent.intent_id,
      } as never);
    });

    const result = await resultPromise;
    expect(result).not.toBeNull();
    expect(result).toMatchObject({ ok: true, frame: { folder: { folder_ref: "f-new" } } });
    unsubscribe();
  });

  it("resolves { ok: false } on a rejection instead of waiting for a ref", async () => {
    vi.resetModules();
    const { sessionSurfaceRegistry } = await import("../src/lib/sessionSurfaceRegistry");
    const unsubscribe = sessionSurfaceRegistry.subscribeConnection(() => {});
    await waitFor(() => expect(sessionSurfaceRegistry.isSocketOpen()).toBe(true));

    const resultPromise = sessionSurfaceRegistry.submitIntentAwaitingRef(
      { kind: "create_folder", session_id: null, project_ref: "/p", name: "", parent_folder_ref: null },
      isFolderUpsert,
    );
    const sent = ws.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "create_folder",
    ) as { intent: { intent_id: string } };

    act(() => {
      ws.emit({ type: "intent_rejected", intent_id: sent.intent.intent_id, code: "invalid", message: "name required" } as never);
    });

    const result = await resultPromise;
    expect(result).toEqual({ ok: false, code: "invalid", message: "name required" });
    unsubscribe();
  });

  it("resolves { ok: true, frame: undefined } if no matching upsert arrives before the window elapses", async () => {
    vi.resetModules();
    const { sessionSurfaceRegistry } = await import("../src/lib/sessionSurfaceRegistry");
    const unsubscribe = sessionSurfaceRegistry.subscribeConnection(() => {});
    await waitFor(() => expect(sessionSurfaceRegistry.isSocketOpen()).toBe(true));

    vi.useFakeTimers();
    try {
      const resultPromise = sessionSurfaceRegistry.submitIntentAwaitingRef(
        { kind: "create_folder", session_id: null, project_ref: "/p", name: "New", parent_folder_ref: null },
        isFolderUpsert,
      );
      const sent = ws.outbound.find(
        (f) => (f as { intent?: { kind?: string } }).intent?.kind === "create_folder",
      ) as { intent: { intent_id: string } };

      act(() => {
        ws.emit({ type: "intent_accepted", intent_id: sent.intent.intent_id } as never);
      });
      // No `folder_upsert` ever arrives — advance past both the late-
      // rejection window and the ref-correlation window.
      await vi.advanceTimersByTimeAsync(2000);

      const result = await resultPromise;
      expect(result).toEqual({ ok: true, frame: undefined });
    } finally {
      vi.useRealTimers();
    }
    unsubscribe();
  });
});
