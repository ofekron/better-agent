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

type Pending = { url: string; resolve: (body: unknown) => void };

function installFetchStub() {
  const pending: Pending[] = [];
  const stub = vi.fn(async (input: RequestInfo | URL): Promise<Response> => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
        ? input.toString()
        : input.url;
    return new Promise<Response>((res) => {
      pending.push({
        url,
        resolve: (body) =>
          res(
            new Response(JSON.stringify(body), {
              status: 200,
              headers: { "content-type": "application/json" },
            }),
          ),
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
  };
}

const EMPTY_PAGE = { sessions: [], has_more: false, snapshot_complete: true };

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  window.localStorage.clear();
});

describe("sessions list background refresh is silent", () => {
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
      eventBus.publish("session_monitoring_changed", {
        session_id: "s1",
        monitoring_state: "active",
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
    await gate.resolveOldestList(EMPTY_PAGE);
    expect(result.current.sessionsSearching).toBe(false);
  });
});
