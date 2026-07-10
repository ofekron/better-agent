/**
 * Regression test — archiving a session gives the user a "time to
 * regret" grace window before the archive PUT actually fires.
 *
 * Before this change, `archiveSession(id, true)` sent the backend PUT
 * immediately. An accidental click was permanent the instant it landed
 * on the server. Now the click marks the session `archivePending`
 * locally and only fires the PUT after `ARCHIVE_GRACE_MS`; calling
 * `archiveSession(id, false)` during that window cancels the pending
 * timer and never touches the backend at all.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useSession, ARCHIVE_GRACE_MS } from "../src/hooks/useSession";
import type { Session } from "../src/types";

const SESSION_LIST = /\/api\/sessions\?/;
const ARCHIVE_PUT = /\/api\/sessions\/a\/archive$/;

function makeSession(overrides: Partial<Session> = {}): Session {
  const now = new Date().toISOString();
  return {
    id: "a",
    name: "Alpha",
    model: "claude-sonnet-4-6",
    cwd: "/tmp/proj",
    orchestration_mode: "manager",
    created_at: now,
    updated_at: now,
    archived: false,
    messages: [],
    ...overrides,
  };
}

describe("useSession.archiveSession — grace window", () => {
  let realFetch: typeof fetch;
  let fetchMock: ReturnType<typeof vi.fn>;

  afterEach(() => {
    vi.useRealTimers();
    globalThis.fetch = realFetch;
  });

  function installFetch(session: Session) {
    realFetch = globalThis.fetch;
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      const body = SESSION_LIST.test(url)
        ? { sessions: [session] }
        : { id: session.id, archived: true };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  }

  it("does not PUT immediately, and an undo within the grace window cancels it entirely", async () => {
    installFetch(makeSession());
    vi.useFakeTimers();

    const { result } = renderHook(() => useSession());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.sessions.map((s) => s.id)).toEqual(["a"]);

    await act(async () => {
      void result.current.archiveSession("a", true);
      await Promise.resolve();
    });

    expect(result.current.sessions.find((s) => s.id === "a")?.archivePending).toBe(true);
    expect(result.current.sessions.find((s) => s.id === "a")?.archived).toBe(false);
    expect(fetchMock.mock.calls.some(([u]) => ARCHIVE_PUT.test(String(u)))).toBe(false);

    // Undo before the grace window elapses.
    await act(async () => {
      void result.current.archiveSession("a", false);
      await Promise.resolve();
    });
    expect(result.current.sessions.find((s) => s.id === "a")?.archivePending).toBe(false);
    expect(result.current.sessions.find((s) => s.id === "a")?.archived).toBe(false);

    // Even after the original grace window would have elapsed, no PUT
    // was ever sent — the timer was cancelled, not just delayed.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ARCHIVE_GRACE_MS + 1000);
    });
    expect(fetchMock.mock.calls.some(([u]) => ARCHIVE_PUT.test(String(u)))).toBe(false);
  });

  it("commits the archive PUT once the grace window elapses without an undo", async () => {
    installFetch(makeSession());
    vi.useFakeTimers();

    const { result } = renderHook(() => useSession());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    await act(async () => {
      void result.current.archiveSession("a", true);
      await Promise.resolve();
    });
    expect(result.current.sessions.find((s) => s.id === "a")?.archivePending).toBe(true);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(ARCHIVE_GRACE_MS + 50);
    });

    expect(fetchMock.mock.calls.some(([u]) => ARCHIVE_PUT.test(String(u)))).toBe(true);
    expect(result.current.sessions.find((s) => s.id === "a")?.archived).toBe(true);
    expect(result.current.sessions.find((s) => s.id === "a")?.archivePending).toBe(false);
  });
});
