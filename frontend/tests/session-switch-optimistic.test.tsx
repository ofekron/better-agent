/**
 * Regression test — `selectSession` optimistic swap.
 *
 * Invariant: clicking a different session must flip `currentSession`
 * to the new id in the SAME tick as the click, BEFORE awaiting the
 * REST `/api/sessions/:id?exchange_count=N` round-trip. Older code
 * awaited REST first, so the sidebar highlight (`currentSession?.id`)
 * and the chat view (driven by the same value) sat on the old
 * session for the entire RTT — the user perceived "clicking a
 * session does nothing for a beat", especially during active turns
 * when the backend's REST handler contends with WS-frame work.
 *
 * Also locks: a stale prior `selectSession` REST response cannot
 * clobber a newer optimistic state. With back-to-back clicks A→B,
 * if A's REST returns AFTER B is in place, A's tree must be
 * discarded via `selectRequestIdRef`.
 *
 * Tested at the hook level (renderHook) — the App-level harness is
 * blocked behind an unmocked `/api/auth/me` gate, but the changed
 * contract lives in `useSession.selectSession` and is exercised
 * directly here.
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useSession } from "../src/hooks/useSession";
import { isInflight } from "../src/progress/store";
import type { Session } from "../src/types";

const SESSION_FETCH = /\/api\/sessions\/[^?]+\?.*exchange_count=/;
const SESSION_LIST = /\/api\/sessions\?/;
const SESSION_DELETE = /\/api\/sessions\/[^/?]+$/;

function makeSession(overrides: Partial<Session> = {}): Session {
  const now = new Date().toISOString();
  return {
    id: "sess",
    name: "session",
    model: "claude-sonnet-4-6",
    cwd: "/tmp/proj",
    orchestration_mode: "manager",
    created_at: now,
    updated_at: now,
    messages: [],
    ...overrides,
  };
}

type Resolver = (body: unknown, status?: number) => void;
interface FetchGate {
  /** Records every URL fetch was called with. */
  readonly urls: string[];
  readonly inits: (RequestInit | undefined)[];
  /** Resolve the OLDEST pending fetch for paths matching `pattern`
   *  with the given JSON body. Throws if none is pending. */
  resolve(pattern: RegExp, body: unknown, status?: number): void;
  /** True when at least one fetch is parked waiting on a manual
   *  resolve. */
  hasPending(pattern: RegExp): boolean;
  /** Tear down — restores the real fetch. */
  restore(): void;
}

function installFetchGate(opts: {
  /** Paths matching this regex are HELD until resolve() is called. */
  hold: RegExp;
  /** Default response body for every non-held URL. */
  defaultBody?: unknown | ((url: string) => unknown);
  /** Called SYNCHRONOUSLY at the moment fetch is invoked, before the
   *  returned promise is awaited by anything. Lets a caller capture
   *  same-tick observable state (e.g. read renderHook result.current
   *  to prove a state mutation already happened before fetch ran). */
  onCall?: (url: string) => void;
}): FetchGate {
  const realFetch = globalThis.fetch;
  const urls: string[] = [];
  const inits: (RequestInit | undefined)[] = [];
  const pending: { pattern: RegExp; resolver: Resolver }[] = [];

  const wrapper = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
          ? input.toString()
          : input.url;
      urls.push(url);
      inits.push(init);
      opts.onCall?.(url);
      if (opts.hold.test(url)) {
        return new Promise<Response>((res) => {
          pending.push({
            pattern: opts.hold,
            resolver: (body, status = 200) =>
              res(
                new Response(JSON.stringify(body), {
                  status,
                  headers: { "content-type": "application/json" },
                }),
              ),
          });
        });
      }
      const body =
        typeof opts.defaultBody === "function"
          ? opts.defaultBody(url)
          : opts.defaultBody ?? {};
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  );
  globalThis.fetch = wrapper as unknown as typeof fetch;

  return {
    urls,
    inits,
    resolve(pattern, body, status = 200) {
      const idx = pending.findIndex((p) => pattern.source === p.pattern.source);
      if (idx < 0) {
        throw new Error(
          `installFetchGate: no pending request matched ${pattern.source}`,
        );
      }
      const [{ resolver }] = pending.splice(idx, 1);
      resolver(body, status);
    },
    hasPending(pattern) {
      return pending.some((p) => pattern.source === p.pattern.source);
    },
    restore() {
      globalThis.fetch = realFetch;
    },
  };
}

describe("useSession.selectSession — optimistic swap", () => {
  let gate: FetchGate | null = null;
  afterEach(() => {
    if (gate) {
      gate.restore();
      gate = null;
    }
  });

  it("flips currentSession to the clicked session BEFORE the REST round-trip resolves", async () => {
    const a = makeSession({ id: "a", name: "Alpha" });
    const b = makeSession({ id: "b", name: "Beta" });

    // Initial /api/sessions for the sidebar list resolves immediately.
    // /api/sessions/:id requests are HELD so we can observe what
    // currentSession looks like with REST still in flight.
    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: { sessions: [a, b] },
    });

    const { result } = renderHook(() => useSession());

    // Sidebar fetch lands.
    await waitFor(() => {
      expect(result.current.sessions.map((s) => s.id).sort()).toEqual([
        "a",
        "b",
      ]);
    });
    expect(result.current.currentSession).toBeNull();

    // Click session "b" — REST is held, but currentSession MUST flip
    // optimistically to the cached sidebar entry.
    await act(async () => {
      void result.current.selectSession("b");
      // Yield a microtask so the synchronous setState commits.
      await Promise.resolve();
    });

    expect(gate.hasPending(SESSION_FETCH)).toBe(true);
    expect(result.current.currentSession?.id).toBe("b");
    expect(result.current.currentSession?.name).toBe("Beta");
    // Optimistic stub carries empty messages until REST lands.
    expect(result.current.currentSession?.messages).toEqual([]);

    // Now release the REST response with the canonical (full) tree.
    const canonicalB: Session = {
      ...b,
      messages: [
        {
          id: "m1",
          role: "user",
          content: "hello",
          events: [],
          timestamp: new Date().toISOString(),
          isStreaming: false,
        },
      ],
    };
    await act(async () => {
      gate!.resolve(SESSION_FETCH, canonicalB);
      await Promise.resolve();
    });

    expect(result.current.currentSession?.id).toBe("b");
    expect(result.current.currentSession?.messages).toHaveLength(1);
  });

  it("pinning a selected session updates the current tree and sidebar row", async () => {
    const a = makeSession({ id: "a", name: "Alpha", pinned: false });
    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: (url) => (
        url.endsWith("/api/sessions/a/pin")
          ? { id: "a", pinned: true }
          : { sessions: [a] }
      ),
    });

    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.sessions.map((s) => s.id)).toEqual(["a"]);
    });

    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
    });
    await act(async () => {
      gate!.resolve(SESSION_FETCH, a);
      await Promise.resolve();
    });

    await act(async () => {
      await result.current.togglePin("a", true);
    });

    expect(result.current.currentSession?.pinned).toBe(true);
    expect(result.current.sessions.find((s) => s.id === "a")?.pinned).toBe(true);
  });

  it("unpinning other sessions updates a selected affected session", async () => {
    const a = makeSession({ id: "a", name: "Alpha", pinned: true });
    const b = makeSession({ id: "b", name: "Beta", pinned: true });
    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: (url) => (
        url.endsWith("/api/sessions/a/unpin-others")
          ? { id: "a", unpinned_ids: ["b"], count: 1 }
          : { sessions: [a, b] }
      ),
    });

    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.sessions.map((s) => s.id).sort()).toEqual(["a", "b"]);
    });

    await act(async () => {
      void result.current.selectSession("b");
      await Promise.resolve();
    });
    await act(async () => {
      gate!.resolve(SESSION_FETCH, b);
      await Promise.resolve();
    });

    await act(async () => {
      await result.current.unpinOtherSessions("a");
    });

    expect(result.current.currentSession?.id).toBe("b");
    expect(result.current.currentSession?.pinned).toBe(false);
    expect(result.current.sessions.find((s) => s.id === "b")?.pinned).toBe(false);
  });

  it("does not abort slow selected-session fetches with the offline timeout", async () => {
    const a = makeSession({ id: "a", name: "Alpha" });
    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: { sessions: [a] },
    });

    const { result } = renderHook(() => useSession());

    await waitFor(() => {
      expect(result.current.sessions.map((s) => s.id)).toEqual(["a"]);
    });

    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
    });

    const detailIndex = gate.urls.findIndex((url) => SESSION_FETCH.test(url));
    expect(detailIndex).toBeGreaterThanOrEqual(0);
    expect(gate.inits[detailIndex]?.signal).toBeUndefined();
    expect(result.current.sessionLoadError).toBeNull();
    expect(gate.hasPending(SESSION_FETCH)).toBe(true);
  });

  it("removes a deleted session before the DELETE round-trip resolves", async () => {
    const a = makeSession({ id: "a", name: "Alpha" });
    const b = makeSession({ id: "b", name: "Beta" });
    gate = installFetchGate({
      hold: SESSION_DELETE,
      defaultBody: (url) => {
        if (SESSION_FETCH.test(url)) return a;
        return { sessions: [a, b] };
      },
    });

    const { result } = renderHook(() => useSession());

    await waitFor(() => {
      expect(result.current.sessions.map((s) => s.id)).toEqual(["a", "b"]);
    });
    await act(async () => {
      await result.current.selectSession("a");
    });
    expect(result.current.currentSession?.id).toBe("a");

    let deletion: Promise<void>;
    await act(async () => {
      deletion = result.current.deleteSession("a");
      await Promise.resolve();
    });

    expect(gate.hasPending(SESSION_DELETE)).toBe(true);
    expect(result.current.sessions.map((s) => s.id)).toEqual(["b"]);
    expect(result.current.currentSession).toBeNull();

    await act(async () => {
      gate!.resolve(SESSION_DELETE, { deleted: true });
      await deletion!;
    });

    expect(result.current.sessions.map((s) => s.id)).toEqual(["b"]);
  });

  it("repairs optimistic deletion when the backend rejects the delete", async () => {
    const a = makeSession({ id: "a", name: "Alpha" });
    const b = makeSession({ id: "b", name: "Beta" });
    gate = installFetchGate({
      hold: SESSION_DELETE,
      defaultBody: (url) => {
        if (SESSION_FETCH.test(url)) return a;
        return { sessions: [a, b] };
      },
    });

    const { result } = renderHook(() => useSession());

    await waitFor(() => {
      expect(result.current.sessions.map((s) => s.id)).toEqual(["a", "b"]);
    });
    await act(async () => {
      await result.current.selectSession("a");
    });
    expect(result.current.currentSession?.id).toBe("a");

    let deletion: Promise<void>;
    await act(async () => {
      deletion = result.current.deleteSession("a");
      await Promise.resolve();
    });

    expect(result.current.sessions.map((s) => s.id)).toEqual(["b"]);
    expect(result.current.currentSession).toBeNull();

    await act(async () => {
      gate!.resolve(SESSION_DELETE, { detail: "nope" }, 500);
      await expect(deletion!).rejects.toThrow("nope");
    });

    await waitFor(() => {
      expect(result.current.sessions.map((s) => s.id)).toEqual(["a", "b"]);
      expect(result.current.currentSession?.id).toBe("a");
    });
  });

  it("composes same-tick functional metadata updates from the latest session state", async () => {
    const session = makeSession({ id: "a", inline_tags: [] });
    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: { sessions: [session] },
    });

    const { result } = renderHook(() => useSession());

    await waitFor(() => {
      expect(result.current.sessions.map((s) => s.id)).toEqual(["a"]);
    });

    await act(async () => {
      result.current.applySessionMetadata("a", (s) => ({
        inline_tags: [
          ...(s.inline_tags ?? []),
          {
            id: "tag-1",
            messageId: "__file__/tmp/a.md",
            selectedText: "",
            comment: "first",
            timestamp: "2026-01-01T00:00:00.000Z",
            fileAnchor: { filePath: "/tmp/a.md" },
          },
        ],
      }));
      result.current.applySessionMetadata("a", (s) => ({
        inline_tags: [
          ...(s.inline_tags ?? []),
          {
            id: "tag-2",
            messageId: "__file__/tmp/a.md",
            selectedText: "",
            comment: "second",
            timestamp: "2026-01-01T00:00:01.000Z",
            fileAnchor: { filePath: "/tmp/a.md" },
          },
        ],
      }));
    });

    expect(result.current.sessions[0].inline_tags?.map((t) => t.comment)).toEqual([
      "first",
      "second",
    ]);
  });

  it("discards a late-returning prior-REST response so it cannot clobber newer optimistic state", async () => {
    const a = makeSession({ id: "race-a", name: "Alpha" });
    const b = makeSession({ id: "race-b", name: "Beta" });

    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: { sessions: [a, b] },
    });

    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.sessions).toHaveLength(2);
    });

    // Click A — REST A is parked.
    await act(async () => {
      void result.current.selectSession("race-a");
      await Promise.resolve();
    });
    expect(result.current.currentSession?.id).toBe("race-a");

    // Click B — REST B is parked. Optimistic state is now B.
    await act(async () => {
      void result.current.selectSession("race-b");
      await Promise.resolve();
    });
    expect(result.current.currentSession?.id).toBe("race-b");

    // Resolve REST A FIRST (out of order). The stale-request guard
    // (selectRequestIdRef) must drop it — currentSession stays on B.
    const canonicalA: Session = { ...a, messages: [] };
    await act(async () => {
      gate!.resolve(SESSION_FETCH, canonicalA);
      await Promise.resolve();
    });
    expect(result.current.currentSession?.id).toBe("race-b");
    expect(isInflight("session:select:race-a")).toBe(false);

    // Resolve REST B — canonical state lands.
    const canonicalB: Session = { ...b, messages: [] };
    await act(async () => {
      gate!.resolve(SESSION_FETCH, canonicalB);
      await Promise.resolve();
    });
    expect(result.current.currentSession?.id).toBe("race-b");
    expect(isInflight("session:select:race-b")).toBe(false);
  });

  it("DEEP-LINK (no cached entry): no optimistic write — currentSession stays null until REST resolves", async () => {
    // selectSession can be called with an id not in the sidebar list
    // (initial route restoration before /api/sessions returns, fork id
    // that lives only inside the tree, manual URL paste). The
    // optimistic write must skip this case — there's nothing safe to
    // stub from.
    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: { sessions: [] }, // empty sidebar
    });

    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.sessions).toHaveLength(0);
    });

    await act(async () => {
      void result.current.selectSession("unknown-id");
      await Promise.resolve();
    });

    // No cached entry → no optimistic stub. currentSession stays null
    // until REST returns the canonical tree.
    expect(result.current.currentSession).toBeNull();
    expect(gate.hasPending(SESSION_FETCH)).toBe(true);

    const canonical = makeSession({ id: "unknown-id", name: "Surprise" });
    await act(async () => {
      gate!.resolve(SESSION_FETCH, canonical);
      await Promise.resolve();
    });
    expect(result.current.currentSession?.id).toBe("unknown-id");
  });

  it("REFETCH-OF-SAME-ID: optimistic stub does NOT wipe loaded messages", async () => {
    // The most dangerous regression to guard against. If the optimistic
    // path stubbed the currently-focused session on every selectSession
    // call, a refetch (e.g. post-turn refresh) would briefly blank the
    // chat. The `cur?.id !== id` guard prevents this.
    const a = makeSession({ id: "a", name: "Alpha" });

    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: { sessions: [a] },
    });

    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.sessions).toHaveLength(1);
    });

    // First select: optimistic stub → REST resolves with canonical
    // messages.
    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
    });
    const canonicalA: Session = {
      ...a,
      messages: [
        {
          id: "m1",
          role: "user",
          content: "first",
          events: [],
          timestamp: new Date().toISOString(),
          isStreaming: false,
        },
      ],
    };
    await act(async () => {
      gate!.resolve(SESSION_FETCH, canonicalA);
      await Promise.resolve();
    });
    expect(result.current.currentSession?.messages).toHaveLength(1);

    // Second select with the SAME id (refetch). Optimistic guard
    // must keep loaded messages in place — no flash to empty.
    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
    });
    expect(result.current.currentSession?.id).toBe("a");
    expect(result.current.currentSession?.messages).toHaveLength(1);
  });

  it("paints a cached session immediately, then reconciles it with backend truth", async () => {
    const a = makeSession({ id: "a", name: "Alpha" });
    const b = makeSession({ id: "b", name: "Beta" });

    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: { sessions: [a, b] },
    });

    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.sessions).toHaveLength(2);
    });

    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
    });
    const failedAssistant = {
      id: "a-failed",
      role: "assistant" as const,
      content: "",
      events: [],
      timestamp: "2026-07-28T09:46:37.601488",
      isStreaming: false,
      seq: 11,
      errorText: "selected extension MCP 'scheduler' exposed no usable tools",
    };
    await act(async () => {
      gate!.resolve(SESSION_FETCH, {
        ...a,
        messages: [failedAssistant],
      });
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(result.current.currentSession?.messages?.[0]?.id).toBe("a-failed");
    });

    await act(async () => {
      void result.current.selectSession("b");
      await Promise.resolve();
    });
    await act(async () => {
      gate!.resolve(SESSION_FETCH, { ...b, messages: [] });
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(result.current.currentSession?.id).toBe("b");
    });

    const fetchCountBeforeReopen = gate.urls.filter((u) => SESSION_FETCH.test(u)).length;
    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
    });

    expect(result.current.currentSession?.id).toBe("a");
    expect(result.current.currentSession?.messages?.map((message) => message.id)).toEqual([
      "a-failed",
    ]);
    expect(result.current.wsTargetSessionId).toBeNull();
    expect(gate.urls.filter((u) => SESSION_FETCH.test(u))).toHaveLength(
      fetchCountBeforeReopen + 1,
    );
    expect(gate.hasPending(SESSION_FETCH)).toBe(true);

    const persistedPrompt = {
      id: "a-user",
      role: "user" as const,
      content: "persisted prompt",
      events: [],
      timestamp: "2026-07-28T09:46:37.587603",
      isStreaming: false,
      seq: 10,
    };
    await act(async () => {
      gate!.resolve(SESSION_FETCH, {
        ...a,
        messages: [persistedPrompt, failedAssistant],
      });
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(result.current.currentSession?.messages?.map((message) => message.id)).toEqual([
        "a-user",
        "a-failed",
      ]);
      expect(result.current.wsTargetSessionId).toBe("a");
      expect(result.current.sessionLoading).toBe(false);
    });
  });

  it("keeps cached content visible and disconnected when reconciliation fails", async () => {
    const a = makeSession({ id: "a", name: "Alpha" });
    const b = makeSession({ id: "b", name: "Beta" });
    const cachedMessage = {
      id: "a-cached",
      role: "user" as const,
      content: "cached prompt",
      events: [],
      timestamp: "2026-07-28T09:46:37.587603",
      isStreaming: false,
      seq: 10,
    };

    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: { sessions: [a, b] },
    });

    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.sessions).toHaveLength(2);
    });

    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
    });
    await act(async () => {
      gate!.resolve(SESSION_FETCH, { ...a, messages: [cachedMessage] });
      await Promise.resolve();
    });

    await act(async () => {
      void result.current.selectSession("b");
      await Promise.resolve();
    });
    await act(async () => {
      gate!.resolve(SESSION_FETCH, { ...b, messages: [] });
      await Promise.resolve();
    });

    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
    });
    expect(result.current.currentSession?.messages?.[0]?.id).toBe("a-cached");
    expect(result.current.wsTargetSessionId).toBeNull();

    await act(async () => {
      gate!.resolve(SESSION_FETCH, { detail: "backend unavailable" }, 503);
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(result.current.currentSession?.messages?.[0]?.id).toBe("a-cached");
      expect(result.current.sessionLoadError).toEqual({
        sessionId: "a",
        message: "backend unavailable",
      });
      expect(result.current.wsTargetSessionId).toBeNull();
      expect(result.current.sessionLoading).toBe(false);
    });
  });

  it("retains a late replay for A while B loads and applies it exactly once", async () => {
    const aPrompt = {
      id: "a-user",
      role: "user" as const,
      content: "first prompt",
      events: [],
      timestamp: "2026-07-28T09:46:37.000000",
      isStreaming: false,
      seq: 0,
    };
    const lateReply = {
      id: "a-late",
      role: "assistant" as const,
      content: "late replay",
      events: [],
      timestamp: "2026-07-28T09:46:38.000000",
      isStreaming: false,
      seq: 1,
    };
    const a = makeSession({ id: "a", name: "Alpha", messages: [aPrompt] });
    const b = makeSession({ id: "b", name: "Beta" });

    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: { sessions: [a, b] },
    });

    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.sessions).toHaveLength(2);
    });

    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
      gate!.resolve(SESSION_FETCH, a);
      await Promise.resolve();
    });

    await act(async () => {
      void result.current.selectSession("b");
      await Promise.resolve();
      result.current.applyMessagesReplay("a", [lateReply]);
      gate!.resolve(SESSION_FETCH, b);
      await Promise.resolve();
    });

    expect(result.current.currentSession?.id).toBe("b");
    expect(result.current.getSinceSeq("a")).toBe(0);

    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
      gate!.resolve(SESSION_FETCH, a);
      await Promise.resolve();
    });

    expect(result.current.currentSession?.messages?.map((message) => message.id)).toEqual([
      "a-user",
      "a-late",
    ]);
    expect(result.current.getSinceSeq("a")).toBe(1);

    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
      gate!.resolve(SESSION_FETCH, {
        ...a,
        messages: [aPrompt, lateReply],
      });
      await Promise.resolve();
    });

    expect(result.current.currentSession?.messages?.map((message) => message.id)).toEqual([
      "a-user",
      "a-late",
    ]);
  });

  it("REFETCH-OF-SAME-ID: finalized REST snapshot replaces stale local streaming message", async () => {
    const a = makeSession({ id: "a", name: "Alpha" });

    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: { sessions: [a] },
    });

    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.sessions).toHaveLength(1);
    });

    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
    });
    const streamingA: Session = {
      ...a,
      messages: [
        {
          id: "u1",
          role: "user",
          content: "prompt",
          events: [],
          timestamp: new Date().toISOString(),
          isStreaming: false,
          seq: 0,
        },
        {
          id: "a1",
          role: "assistant",
          content: "partial",
          events: [],
          timestamp: new Date().toISOString(),
          isStreaming: true,
          seq: 1,
        },
      ],
    };
    await act(async () => {
      gate!.resolve(SESSION_FETCH, streamingA);
      await Promise.resolve();
    });
    expect(result.current.currentSession?.messages?.[1]?.isStreaming).toBe(true);

    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
    });
    const finalizedA: Session = {
      ...a,
      messages: [
        streamingA.messages![0],
        {
          ...streamingA.messages![1],
          content: "complete",
          isStreaming: false,
        },
      ],
    };
    await act(async () => {
      gate!.resolve(SESSION_FETCH, finalizedA);
      await Promise.resolve();
    });

    expect(result.current.currentSession?.messages?.[1]?.content).toBe("complete");
    expect(result.current.currentSession?.messages?.[1]?.isStreaming).toBe(false);
  });

  it("REFETCH-OF-SAME-ID: completed backend truth removes stale baseline rows", async () => {
    const a = makeSession({ id: "a", name: "Alpha" });
    const userMessage = {
      id: "u1",
      role: "user" as const,
      content: "prompt",
      events: [],
      timestamp: "2026-07-28T09:46:37.587603",
      isStreaming: false,
      seq: 10,
    };
    const staleFailure = {
      id: "stale-failure",
      role: "assistant" as const,
      content: "",
      events: [],
      timestamp: "2026-07-28T09:46:37.601488",
      isStreaming: false,
      seq: 11,
      errorText: "stale failure",
    };
    const canonicalAssistant = {
      id: "canonical-assistant",
      role: "assistant" as const,
      content: "complete",
      events: [],
      timestamp: "2026-07-28T09:46:38.000000",
      isStreaming: false,
      seq: 11,
    };

    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: { sessions: [a] },
    });
    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.sessions).toHaveLength(1);
    });

    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
    });
    await act(async () => {
      gate!.resolve(SESSION_FETCH, {
        ...a,
        messages: [userMessage, staleFailure],
      });
      await Promise.resolve();
    });

    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
    });
    await act(async () => {
      gate!.resolve(SESSION_FETCH, {
        ...a,
        messages: [userMessage, canonicalAssistant],
      });
      await Promise.resolve();
    });

    expect(result.current.currentSession?.messages?.map((message) => message.id)).toEqual([
      "u1",
      "canonical-assistant",
    ]);
  });

  it("session reconciliation is authoritative without a refresh correlation id", async () => {
    const stale = {
      id: "stale-orphan",
      role: "assistant" as const,
      content: "orphaned local row",
      events: [],
      timestamp: "2026-07-28T09:46:37.000000",
      isStreaming: false,
      seq: 4,
    };
    const prompt = {
      id: "restored-prompt",
      role: "user" as const,
      content: "restored prompt",
      events: [],
      timestamp: "2026-07-28T09:46:36.000000",
      isStreaming: false,
      seq: 0,
    };
    const a = makeSession({ id: "a", messages: [stale] });

    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: { sessions: [a] },
    });
    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.sessions).toHaveLength(1);
    });

    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
      gate!.resolve(SESSION_FETCH, a);
      await Promise.resolve();
    });

    await act(async () => {
      void result.current.applySessionReconciled("a");
      await Promise.resolve();
      gate!.resolve(SESSION_FETCH, { ...a, messages: [prompt] });
      await Promise.resolve();
    });

    expect(result.current.currentSession?.messages?.map((message) => message.id)).toEqual([
      "restored-prompt",
    ]);
  });

  it("does not preserve an empty terminal assistant removed by authoritative recovery", async () => {
    const userMessage = {
      id: "u1",
      role: "user" as const,
      content: "retry prompt",
      events: [],
      timestamp: "2026-07-28T03:16:35.981300",
      isStreaming: false,
      seq: 6,
      lifecycle_msg_id: "lifecycle-u1",
    };
    const emptyAssistant = {
      id: "a1",
      role: "assistant" as const,
      content: "",
      events: [],
      workers: [],
      timestamp: "2026-07-28T03:16:36.000000",
      isStreaming: true,
      seq: 7,
    };
    const recoveredUser = {
      ...userMessage,
      status: "error" as const,
      errorText: "Backend stopped before the provider run started.",
    };
    const a = makeSession({
      id: "a",
      messages: [userMessage, emptyAssistant],
    });

    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: { sessions: [a] },
    });
    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.sessions).toHaveLength(1);
    });

    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
      gate!.resolve(SESSION_FETCH, a);
      await Promise.resolve();
    });

    await act(async () => {
      void result.current.applySessionReconciled("a");
      await Promise.resolve();
      result.current.markTurnTerminal("a", "2026-07-28T03:17:00.000000");
      result.current.patchMessageStatus("a", "lifecycle-u1", "done");
      gate!.resolve(SESSION_FETCH, {
        ...a,
        messages: [recoveredUser],
      });
      await Promise.resolve();
    });

    expect(result.current.currentSession?.messages).toEqual([recoveredUser]);
  });

  it("preserves an empty terminal assistant created during an older REST request", async () => {
    const userMessage = {
      id: "u1",
      role: "user" as const,
      content: "prompt",
      events: [],
      timestamp: "2026-07-28T03:16:35.981300",
      isStreaming: false,
      seq: 6,
    };
    const emptyAssistant = {
      id: "a1",
      role: "assistant" as const,
      content: "",
      events: [],
      workers: [],
      timestamp: "2026-07-28T03:16:36.000000",
      isStreaming: true,
      seq: 7,
    };
    const a = makeSession({ id: "a", messages: [userMessage] });

    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: { sessions: [a] },
    });
    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.sessions).toHaveLength(1);
    });
    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
      gate!.resolve(SESSION_FETCH, a);
      await Promise.resolve();
    });

    await act(async () => {
      void result.current.applySessionReconciled("a");
      await Promise.resolve();
      result.current.addMessages("a", [emptyAssistant]);
      result.current.markTurnTerminal("a", "2026-07-28T03:17:00.000000");
      gate!.resolve(SESSION_FETCH, a);
      await Promise.resolve();
    });

    expect(result.current.currentSession?.messages?.map((message) => message.id)).toEqual([
      "u1",
      "a1",
    ]);
    expect(result.current.currentSession?.messages?.[1]?.isStreaming).toBe(false);
    expect(result.current.currentSession?.messages?.[1]?.content).toBe("");
  });

  it("REFETCH-OF-SAME-ID: preserves messages created or changed after reconciliation starts", async () => {
    const a = makeSession({ id: "a", name: "Alpha" });
    const userMessage = {
      id: "u1",
      role: "user" as const,
      content: "prompt",
      events: [],
      timestamp: "2026-07-28T09:46:37.587603",
      isStreaming: false,
      seq: 10,
    };
    const arrivedDuringFetch = {
      id: "a2",
      role: "assistant" as const,
      content: "arrived during fetch",
      events: [],
      timestamp: "2026-07-28T09:46:38.000000",
      isStreaming: false,
      seq: 12,
    };
    const streamingAssistant = {
      id: "a1",
      role: "assistant" as const,
      content: "before fetch",
      events: [],
      timestamp: "2026-07-28T09:46:37.700000",
      isStreaming: true,
      seq: 11,
    };

    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: { sessions: [a] },
    });
    const { result } = renderHook(() => useSession());
    await waitFor(() => {
      expect(result.current.sessions).toHaveLength(1);
    });

    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
    });
    await act(async () => {
      gate!.resolve(SESSION_FETCH, {
        ...a,
        messages: [userMessage, streamingAssistant],
      });
      await Promise.resolve();
    });

    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
      result.current.addMessages("a", [arrivedDuringFetch]);
      result.current.applyMessageContent("a", "a1", "changed during fetch");
    });
    await act(async () => {
      gate!.resolve(SESSION_FETCH, {
        ...a,
        messages: [userMessage, streamingAssistant],
      });
      await Promise.resolve();
    });

    expect(result.current.currentSession?.messages?.map((message) => message.id)).toEqual([
      "u1",
      "a1",
      "a2",
    ]);
    expect(result.current.currentSession?.messages?.[1]?.content).toBe(
      "changed during fetch",
    );
  });

  it("uses the current search field snapshot when refetching the session list", async () => {
    const a = makeSession({ id: "a", name: "Search title" });
    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: { sessions: [a] },
    });

    const { result } = renderHook(() => useSession());

    await waitFor(() => {
      expect(result.current.sessions.map((s) => s.id)).toEqual(["a"]);
    });

    await act(async () => {
      result.current.setSessionListFilters({
        search: "search",
        searchFields: ["title"],
      });
      await Promise.resolve();
    });

    await waitFor(() => {
      const sessionListUrls = gate!.urls.filter((url) => url.includes("/api/sessions?"));
      expect(sessionListUrls.at(-1)).toContain("search=search");
      expect(sessionListUrls.at(-1)).toContain("search_fields=title");
      expect(sessionListUrls.at(-1)).not.toContain("content");
      expect(sessionListUrls.at(-1)).not.toContain("first_prompt");
    });
  });

  it("omits search fields from session-list requests when search is empty", async () => {
    const a = makeSession({ id: "a", name: "Search title" });
    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: { sessions: [a] },
    });

    const { result } = renderHook(() => useSession());

    await waitFor(() => {
      expect(result.current.sessions.map((s) => s.id)).toEqual(["a"]);
    });

    await act(async () => {
      result.current.setSessionListFilters({
        search: "",
        searchFields: ["content", "title", "first_prompt"],
      });
      await Promise.resolve();
    });

    await waitFor(() => {
      const sessionListUrls = gate!.urls.filter((url) => url.includes("/api/sessions?"));
      expect(sessionListUrls.at(-1)).not.toContain("search=");
      expect(sessionListUrls.at(-1)).not.toContain("search_fields=");
    });
  });

  it("debounces typed searches and rejects the previous query while the latest waits", async () => {
    const initial = makeSession({ id: "initial", name: "Initial" });
    const stale = makeSession({ id: "stale", name: "Stale" });
    gate = installFetchGate({
      hold: SESSION_LIST,
      defaultBody: {},
    });

    const { result } = renderHook(() => useSession());

    await act(async () => {
      gate!.resolve(SESSION_LIST, { sessions: [initial] });
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(result.current.sessions.map((session) => session.id)).toEqual(["initial"]);
    });

    vi.useFakeTimers();
    try {
      act(() => {
        result.current.setSessionListFilters({ search: "old" });
        vi.advanceTimersByTime(300);
      });
      expect(gate.urls.filter((url) => url.includes("search=old"))).toHaveLength(1);

      act(() => {
        result.current.setSessionListFilters({ search: "n" });
        result.current.setSessionListFilters({ search: "ne" });
        result.current.setSessionListFilters({ search: "new" });
      });
      expect(gate.urls.filter((url) => url.includes("search="))).toHaveLength(1);
      expect(result.current.sessionsSearching).toBe(true);

      await act(async () => {
        gate!.resolve(SESSION_LIST, { sessions: [stale] });
        await Promise.resolve();
      });
      expect(result.current.sessions.map((session) => session.id)).toEqual(["initial"]);

      act(() => {
        vi.advanceTimersByTime(299);
      });
      expect(gate.urls.filter((url) => url.includes("search="))).toHaveLength(1);

      act(() => {
        vi.advanceTimersByTime(1);
      });
      expect(gate.urls.filter((url) => url.includes("search=new"))).toHaveLength(1);

      await act(async () => {
        gate!.resolve(SESSION_LIST, { sessions: [initial] });
        await Promise.resolve();
      });
      const requestCountBeforeClear = gate.urls.filter(
        (url) => url.includes("/api/sessions?"),
      ).length;

      act(() => {
        result.current.setSessionListFilters({ search: "later" });
        result.current.setSessionListFilters({ search: "" });
      });
      expect(gate.urls.filter((url) => url.includes("/api/sessions?"))).toHaveLength(
        requestCountBeforeClear + 1,
      );
      expect(gate.urls.at(-1)).not.toContain("search=");

      act(() => {
        vi.advanceTimersByTime(300);
      });
      expect(gate.urls.filter((url) => url.includes("search=later"))).toHaveLength(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the applied search request valid when typing returns to that query", async () => {
    const initial = makeSession({ id: "initial", name: "Initial" });
    gate = installFetchGate({
      hold: SESSION_LIST,
      defaultBody: {},
    });

    const { result } = renderHook(() => useSession());

    await act(async () => {
      gate!.resolve(SESSION_LIST, { sessions: [initial] });
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(result.current.sessions.map((session) => session.id)).toEqual(["initial"]);
    });

    vi.useFakeTimers();
    try {
      act(() => {
        result.current.setSessionListFilters({ search: "applied" });
        vi.advanceTimersByTime(300);
      });
      expect(gate.urls.filter((url) => url.includes("search=applied"))).toHaveLength(1);

      act(() => {
        result.current.setSessionListFilters({ search: "pending" });
        result.current.setSessionListFilters({ search: "applied" });
        vi.advanceTimersByTime(300);
      });
      expect(gate.urls.filter((url) => url.includes("search=applied"))).toHaveLength(2);

      await act(async () => {
        gate!.resolve(SESSION_LIST, { sessions: [initial] });
        await Promise.resolve();
      });
      expect(result.current.sessions.map((session) => session.id)).toEqual(["initial"]);
      expect(result.current.sessionsSearching).toBe(true);

      await act(async () => {
        gate!.resolve(SESSION_LIST, { sessions: [initial] });
        await Promise.resolve();
      });
      expect(result.current.sessionsSearching).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("reconciles search intent entered before the initial session page resolves", async () => {
    const unfiltered = makeSession({ id: "unfiltered", name: "Unfiltered" });
    gate = installFetchGate({
      hold: SESSION_LIST,
      defaultBody: {},
    });

    const { result } = renderHook(() => useSession());

    vi.useFakeTimers();
    try {
      act(() => {
        result.current.setSessionListFilters({ search: "latest" });
      });

      await act(async () => {
        gate!.resolve(SESSION_LIST, { sessions: [unfiltered] }, 500);
        await Promise.resolve();
      });

      act(() => {
        vi.advanceTimersByTime(300);
      });
      expect(gate!.urls.filter((url) => url.includes("search=latest"))).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not locally prepend a created session while search filters are active", async () => {
    const existing = makeSession({ id: "a", name: "Search title" });
    const created = makeSession({ id: "created", name: "Session 12:35" });
    gate = installFetchGate({
      hold: SESSION_FETCH,
      defaultBody: (url) =>
        url.includes("/api/sessions?") ? { sessions: [existing] } : created,
    });

    const { result } = renderHook(() => useSession());

    await waitFor(() => {
      expect(result.current.sessions.map((s) => s.id)).toEqual(["a"]);
    });

    await act(async () => {
      result.current.setSessionListFilters({
        search: "search",
        searchFields: ["title"],
      });
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(gate!.urls.at(-1)).toContain("search_fields=title");
    });

    await act(async () => {
      await result.current.createSession(
        created.name,
        created.model,
        created.cwd,
        created.orchestration_mode,
      );
      await Promise.resolve();
    });

    expect(result.current.sessions.map((s) => s.id)).toEqual(["a"]);
    await waitFor(() => {
      const filteredFetches = gate!.urls.filter(
        (url) =>
          url.includes("/api/sessions?") &&
          url.includes("search=search") &&
          url.includes("search_fields=title"),
      );
      expect(filteredFetches.length).toBeGreaterThanOrEqual(2);
    });
  });

  it("ignores stale session-list responses after search filters change", async () => {
    const matching = makeSession({ id: "match", name: "Search title" });
    const stalePinned = makeSession({
      id: "stale-pinned",
      name: "Session 12:35",
      pinned: true,
    });
    gate = installFetchGate({
      hold: SESSION_LIST,
      defaultBody: {},
    });

    const { result } = renderHook(() => useSession());

    await act(async () => {
      gate!.resolve(SESSION_LIST, { sessions: [matching] });
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(result.current.sessions.map((s) => s.id)).toEqual(["match"]);
    });

    await act(async () => {
      result.current.setSessionListFilters({
        search: "search",
        searchFields: ["content", "title", "first_prompt"],
      });
      await Promise.resolve();
    });

    await act(async () => {
      result.current.setSessionListFilters({
        search: "search",
        searchFields: ["title"],
      });
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(
        gate.urls.filter(
          (url) => url.includes("/api/sessions?") && url.includes("search=search"),
        ),
      ).toHaveLength(2);
    });

    await act(async () => {
      gate!.resolve(SESSION_LIST, { sessions: [stalePinned] });
      await Promise.resolve();
    });
    expect(result.current.sessions.map((s) => s.id)).toEqual(["match"]);

    await act(async () => {
      gate!.resolve(SESSION_LIST, { sessions: [matching] });
      await Promise.resolve();
    });

    expect(result.current.sessions.map((s) => s.id)).toEqual(["match"]);
  });
});
