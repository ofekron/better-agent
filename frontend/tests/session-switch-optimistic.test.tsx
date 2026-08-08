/**
 * Regression test — `useSession.selectSession`'s non-content plane: pin/
 * unpin, delete, same-tick metadata patches, the offline-fetch-abort
 * guard, and session-list search/pagination. All exercised at the hook
 * level (renderHook) since none of this is chat-content rendering.
 *
 * The message-content-reconciliation cases formerly here (the optimistic
 * empty-stub swap; stale-REST-response discard across back-to-back
 * selectSession clicks; REFETCH-OF-SAME-ID cache-preservation guards;
 * applyMessagesReplay/addMessages/applySessionReconciled/markTurnTerminal/
 * patchMessageStatus reconciliation) are DELETED, not ported — every one
 * of those functions is legacy `/ws/chat`-content-plane-only plumbing
 * (wired exclusively in App.tsx's onMessagesReplay/onMessageContentUpdated/
 * onSessionReconciled handlers, confirmed via grep — nothing in
 * src/surface/ references any of them). The native contract-node model
 * has no analogous client-side cache/stub/reconciliation step to guard:
 * each session id gets its own disposable SurfaceStore instance
 * (src/surface/useSurfaceStore.ts), so there is no shared mutable tree
 * for a late response to clobber, no optimistic stub (ChatSurfaceView
 * always renders a loading skeleton until its own store's snapshot fetch
 * resolves — src/surface/ChatSurfaceView.tsx), and reselecting the
 * already-current session id is a complete no-op (the store's owning
 * effect is keyed on sessionId, so nothing refetches). The portable
 * intent — per-session isolation, and a stale snapshot fetch for a
 * session you've navigated away from being unable to clobber the newly
 * selected one — is ported below in the "native surface — session-switch
 * semantics" describe block, against the real App-level renderApp()
 * harness (the "App-level harness is blocked behind an unmocked
 * /api/auth/me gate" note this file's history carried no longer holds —
 * MockBackend answers that route unconditionally).
 */
import { describe, it, expect, afterEach, vi } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useSession } from "../src/hooks/useSession";
import type { Session } from "../src/types";
import { renderApp } from "./harness";
import { makeSession as makeHarnessSession } from "./fixtures";
import { promptNode, turnNode, compactTurn } from "./surface/fixtures";

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

describe("native surface — session-switch semantics (SurfaceStore)", () => {
  it("switching sessions swaps to an isolated store — a session's turns never leak into another", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const a = makeHarnessSession({ id: "a", name: "Alpha", messages: [] });
    const b = makeHarnessSession({ id: "b", name: "Beta", messages: [] });
    const h = await renderApp({
      seed: { sessions: [a, b] },
      configureBackend: (backend) => {
        backend.seedSurface("a", {
          turns: [compactTurn(turnNode("ta", undefined, "a"), promptNode("ta", "from A", "a"), [])],
        });
        backend.seedSurface("b", {
          turns: [compactTurn(turnNode("tb", undefined, "b"), promptNode("tb", "from B", "b"), [])],
        });
      },
    });

    await h.selectSession("a");
    await h.waitFor(() => h.$('[data-testid="surface-typed-prompt"]')?.textContent?.includes("from A"));
    expect(h.$$('[data-testid="surface-turn"]')).toHaveLength(1);

    await h.selectSession("b");
    await h.waitFor(() => h.$('[data-testid="surface-typed-prompt"]')?.textContent?.includes("from B"));
    expect(h.$$('[data-testid="surface-turn"]')).toHaveLength(1);
    expect(h.raw.container.textContent).not.toContain("from A");

    // Reselecting A re-mounts a fresh store and re-fetches — not a stale
    // cache from the first visit.
    await h.selectSession("a");
    await h.waitFor(() => h.$('[data-testid="surface-typed-prompt"]')?.textContent?.includes("from A"));
    expect(h.raw.container.textContent).not.toContain("from B");

    h.unmount();
  });

  it("a snapshot fetch held while navigating away resolves late without clobbering the newly-selected session", async () => {
    // Native counterpart to the deleted legacy "discards a late-returning
    // prior-REST response" case. Each session id owns its own SurfaceStore
    // instance (useSurfaceStore.ts) — navigating away disposes the old one
    // (`cancelled = true`), so a fetch it kicked off that resolves AFTER
    // the disposal is a no-op by construction (state.ts's `hydrate()`:
    // `if (this.cancelled) return;`). There is no shared tree, and so no
    // `selectRequestIdRef`-style request-id arbitration needed to prove.
    localStorage.setItem("ba.surface_native", "1");
    const a = makeHarnessSession({ id: "a", name: "Alpha", messages: [] });
    const b = makeHarnessSession({ id: "b", name: "Beta", messages: [] });
    let releaseA!: () => void;
    const h = await renderApp({
      // "b" first: autoSelectSession picks the FIRST autoselectable
      // session on mount, so this keeps a's snapshot fetch from firing
      // (and resolving unheld) before the hold below is even registered.
      seed: { sessions: [b, a] },
      configureBackend: (backend) => {
        backend.seedSurface("a", {
          turns: [compactTurn(turnNode("ta", undefined, "a"), promptNode("ta", "from A", "a"), [])],
        });
        backend.seedSurface("b", {
          turns: [compactTurn(turnNode("tb", undefined, "b"), promptNode("tb", "from B", "b"), [])],
        });
        // Registered before mount so it is in place for the FIRST ever
        // fetch of a's snapshot, whichever call site triggers it.
        releaseA = backend.holdNext("GET", "/api/v2/surface/sessions/a/snapshot");
      },
    });
    await h.waitFor(() => h.$('[data-testid="surface-typed-prompt"]')?.textContent?.includes("from B"));

    await h.selectSession("a");
    // A's fetch is parked — ChatSurfaceView shows the loading skeleton,
    // not stale/empty content.
    await h.waitFor(() => h.$('[data-testid="surface-loading-skeleton"]') !== null);
    expect(h.$('[data-testid="surface-chat-view"]')).toBeNull();

    await h.selectSession("b");
    await h.waitFor(() => h.$('[data-testid="surface-typed-prompt"]')?.textContent?.includes("from B"));

    // Release A's held fetch now that B is the active session.
    releaseA();
    await h.flush();

    expect(h.$('[data-testid="surface-typed-prompt"]')?.textContent).toContain("from B");
    expect(h.raw.container.textContent).not.toContain("from A");

    h.unmount();
  });

  it("content written to a session while it is not the active view is present once that session becomes active again", async () => {
    // Native counterpart to the deleted legacy "retains a late replay for
    // A while B loads" case. There is no live socket to deliver a frame
    // to for a session that is not currently mounted (its SurfaceStore was
    // disposed on navigating away) — server-side truth updates instead,
    // and a later reselect's fresh REST snapshot picks it up, the same
    // path a cold load or reconnect uses.
    localStorage.setItem("ba.surface_native", "1");
    const a = makeHarnessSession({ id: "a", name: "Alpha", messages: [] });
    const b = makeHarnessSession({ id: "b", name: "Beta", messages: [] });
    const h = await renderApp({
      seed: { sessions: [a, b] },
      configureBackend: (backend) => {
        backend.seedSurface("a", {
          turns: [compactTurn(turnNode("ta", undefined, "a"), promptNode("ta", "first", "a"), [])],
        });
      },
    });

    await h.selectSession("a");
    await h.waitFor(() => h.$('[data-testid="surface-typed-prompt"]')?.textContent?.includes("first"));

    await h.selectSession("b");
    await h.waitFor(() => h.$('[data-testid="surface-chat-view"]') !== null);

    // A second turn is appended to A's server-side truth while A isn't
    // the active view.
    h.backend.seedSurface("a", {
      turns: [
        compactTurn(turnNode("ta", undefined, "a"), promptNode("ta", "first", "a"), []),
        compactTurn(turnNode("ta2", undefined, "a"), promptNode("ta2", "second", "a"), []),
      ],
      identity: { incarnation: "mock", render_rev: 2, hist_rev: 0 },
    });

    await h.selectSession("a");
    await h.waitFor(() => h.$$('[data-testid="surface-turn"]').length === 2);
    // .includes, not exact-equal — PlainUserHeader (TypedPrompt.tsx) now
    // prefixes each plain-user prompt's textContent with the configured
    // display-name header ("test-user" in this harness).
    const prompts = h.$$('[data-testid="surface-typed-prompt"]').map((el) => el.textContent ?? "");
    expect(prompts.some((t) => t.includes("first"))).toBe(true);
    expect(prompts.some((t) => t.includes("second"))).toBe(true);
    expect(prompts).toHaveLength(2);

    h.unmount();
  });
});
