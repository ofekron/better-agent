/**
 * Regression test — periodic sessions-list loading flash.
 *
 * Invariant: the status-churn background refetch (`refetchLoadedSpan`,
 * debounced off live status deltas when statusSort is on) must be
 * SILENT — it must not set `sessionsSearching`, which SessionList
 * renders as the search spinner. Before the fix, every status delta
 * cycle flashed the spinner every ~2.5s. User-initiated fetches
 * (search/filter/refresh) still show the spinner.
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useSession } from "../src/hooks/useSession";
import { eventBus } from "../src/lib/eventBus";

const SESSION_LIST = /\/api\/sessions\?/;

type Pending = {
  url: string;
  resolve: (body: unknown) => void;
  reject: (error: Error) => void;
};

function installFetchStub() {
  const pending: Pending[] = [];
  const stub = vi.fn(async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
        ? input.toString()
        : input.url;
    if (url.endsWith("/healthz")) return { type: "opaque" } as Response;
    return new Promise<Response>((res, reject) => {
      init?.signal?.addEventListener(
        "abort",
        () => reject(new DOMException("Aborted", "AbortError")),
        { once: true },
      );
      pending.push({
        url,
        resolve: (body) =>
          res(
            new Response(JSON.stringify(body), {
              status: 200,
              headers: { "content-type": "application/json" },
            }),
          ),
        reject,
      });
    });
  });
  vi.stubGlobal("fetch", stub);
  return {
    pendingList: () => pending.filter((p) => SESSION_LIST.test(p.url)),
    resolveOldestList: async (body: unknown) => {
      const p = pending.find((q) => SESSION_LIST.test(q.url));
      if (!p) throw new Error("no pending session-list fetch");
      pending.splice(pending.indexOf(p), 1);
      await act(async () => {
        p.resolve(body);
      });
    },
    rejectOldestList: async (error: Error) => {
      const p = pending.find((q) => SESSION_LIST.test(q.url));
      if (!p) throw new Error("no pending session-list fetch");
      pending.splice(pending.indexOf(p), 1);
      await act(async () => {
        p.reject(error);
      });
    },
  };
}

const EMPTY_PAGE = { sessions: [], has_more: false, snapshot_complete: true };

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  window.localStorage.clear();
});

describe("sessions list background refresh is silent", () => {
  it("settles the initial attempt without claiming a failed snapshot loaded", async () => {
    const gate = installFetchStub();
    const { result } = renderHook(() => useSession("authed"));

    await gate.rejectOldestList(new TypeError("Failed to fetch"));

    expect(result.current.sessionsInitialAttemptSettled).toBe(true);
    expect(result.current.sessionsLoaded).toBe(false);
    expect(result.current.sessionInventoryError).toEqual({ kind: "browser_access_blocked" });
  });

  it("settles the initial attempt when the sessions request hangs", async () => {
    vi.useFakeTimers();
    installFetchStub();
    const { result } = renderHook(() => useSession("authed"));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });

    expect(result.current.sessionsInitialAttemptSettled).toBe(true);
    expect(result.current.sessionsLoaded).toBe(false);
    expect(result.current.sessionInventoryError).toEqual({ kind: "unreachable" });
  });

  it("keeps an incomplete snapshot pending until its retry succeeds", async () => {
    vi.useFakeTimers();
    const gate = installFetchStub();
    const { result } = renderHook(() => useSession("authed"));

    await gate.resolveOldestList({
      sessions: [],
      has_more: false,
      snapshot_complete: false,
    });
    expect(result.current.sessionsInitialAttemptSettled).toBe(false);
    expect(result.current.sessionsLoaded).toBe(false);
    expect(result.current.sessionInventoryError).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    await gate.resolveOldestList(EMPTY_PAGE);

    expect(result.current.sessionsInitialAttemptSettled).toBe(true);
    expect(result.current.sessionsLoaded).toBe(true);
    expect(result.current.sessionInventoryError).toBeNull();
  });

  it("preserves loaded sessions while exposing and clearing refresh failure", async () => {
    const gate = installFetchStub();
    const session = { id: "s1", name: "existing" };
    const { result } = renderHook(() => useSession("authed"));

    await gate.resolveOldestList({
      sessions: [session],
      has_more: false,
      snapshot_complete: true,
    });
    expect(result.current.sessionsLoaded).toBe(true);

    let failedRefresh!: Promise<void>;
    act(() => {
      failedRefresh = result.current.refreshSessions();
    });
    await gate.rejectOldestList(new TypeError("Failed to fetch"));
    await act(async () => {
      await failedRefresh;
    });

    expect(result.current.sessionsLoaded).toBe(true);
    expect(result.current.sessionInventoryError).toEqual({ kind: "browser_access_blocked" });
    expect(result.current.sessions.map((item) => item.id)).toEqual(["s1"]);

    let recoveredRefresh!: Promise<void>;
    act(() => {
      recoveredRefresh = result.current.refreshSessions();
    });
    await gate.resolveOldestList({
      sessions: [session],
      has_more: false,
      snapshot_complete: true,
    });
    await act(async () => {
      await recoveredRefresh;
    });
    expect(result.current.sessionInventoryError).toBeNull();
  });

  it("ignores a stale failure after a newer replacement succeeds", async () => {
    const gate = installFetchStub();
    const { result } = renderHook(() => useSession("authed"));

    let newerRefresh!: Promise<void>;
    act(() => {
      newerRefresh = result.current.refreshSessions();
    });
    const pending = gate.pendingList();
    expect(pending).toHaveLength(2);

    await act(async () => {
      pending[1].resolve(EMPTY_PAGE);
    });
    await act(async () => {
      await newerRefresh;
    });
    await act(async () => {
      pending[0].reject(new TypeError("stale failure"));
    });

    expect(result.current.sessionsLoaded).toBe(true);
    expect(result.current.sessionInventoryError).toBeNull();
  });

  it("keeps search loading visible while the server is offline", async () => {
    vi.useFakeTimers();
    const gate = installFetchStub();
    const { result } = renderHook(() => useSession("authed"));

    await gate.resolveOldestList(EMPTY_PAGE);

    act(() => {
      result.current.setSessionListFilters({ search: "offline" });
    });
    expect(result.current.sessionsSearching).toBe(true);

    let staleRefresh!: Promise<void>;
    act(() => {
      staleRefresh = result.current.refreshSessions();
    });
    await gate.resolveOldestList(EMPTY_PAGE);
    await act(async () => {
      await staleRefresh;
    });
    expect(result.current.sessionsSearching).toBe(true);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

    await gate.rejectOldestList(new TypeError("Failed to fetch"));
    expect(result.current.sessionsSearching).toBe(true);

    let refresh!: Promise<void>;
    act(() => {
      refresh = result.current.refreshSessions();
    });
    await gate.resolveOldestList(EMPTY_PAGE);
    await act(async () => {
      await refresh;
    });
    expect(result.current.sessionsSearching).toBe(false);
  });

  it("status-delta refetch does not flip sessionsSearching; user refresh does", async () => {
    vi.useFakeTimers();
    const gate = installFetchStub();
    const { result } = renderHook(() => useSession("authed"));

    // Initial list load.
    await gate.resolveOldestList(EMPTY_PAGE);
    expect(result.current.sessionsLoaded).toBe(true);

    // Enable statusSort — triggers a filter-change refetch (user path).
    act(() => {
      result.current.setSessionListFilters({ statusSort: true });
    });
    await gate.resolveOldestList(EMPTY_PAGE);
    expect(result.current.sessionsSearching).toBe(false);

    // A live status delta debounces the background full-span refetch.
    act(() => {
      eventBus.publish("session_status_changed", {
        session_id: "s1",
        status_key: "running",
        status_rank: 2,
      });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2600);
    });
    // Refetch is now in flight (held by the stub) — spinner must stay off.
    expect(gate.pendingList().length).toBe(1);
    expect(result.current.sessionsSearching).toBe(false);
    await gate.resolveOldestList(EMPTY_PAGE);
    expect(result.current.sessionsSearching).toBe(false);

    // Programmatic refresh (turn completion / WS deltas) is silent too.
    let refresh!: Promise<void>;
    act(() => {
      refresh = result.current.refreshSessions();
    });
    expect(result.current.sessionsSearching).toBe(false);
    await gate.resolveOldestList(EMPTY_PAGE);
    await act(async () => {
      await refresh;
    });

    // Contrast: a user-initiated filter/search change DOES show the spinner.
    act(() => {
      result.current.setSessionListFilters({ statusSort: true, search: "bug" });
    });
    expect(result.current.sessionsSearching).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    await gate.resolveOldestList(EMPTY_PAGE);
    expect(result.current.sessionsSearching).toBe(false);
  });

  it("keeps backend relevance order while a search is active", async () => {
    vi.useFakeTimers();
    const gate = installFetchStub();
    const { result } = renderHook(() => useSession("authed"));
    await gate.resolveOldestList(EMPTY_PAGE);

    act(() => {
      result.current.setSessionListFilters({ search: "bug", statusSort: true });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    await gate.resolveOldestList({
      sessions: [
        { id: "relevant", status_key: "idle", status_rank: 0 },
        { id: "less-relevant", status_key: "error", status_rank: 6 },
      ],
      has_more: false,
      snapshot_complete: true,
    });
    expect(result.current.sessions.map((session) => session.id)).toEqual([
      "relevant",
      "less-relevant",
    ]);

    act(() => {
      eventBus.publish("session_status_changed", {
        session_id: "less-relevant",
        status_key: "error",
        status_rank: 6,
      });
    });
    expect(result.current.sessions.map((session) => session.id)).toEqual([
      "relevant",
      "less-relevant",
    ]);
  });
});
