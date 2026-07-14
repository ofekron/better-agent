import { act, renderHook, waitFor } from '@testing-library/react'
import { StrictMode, useCallback, type ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useCompactTurns } from 'src/hooks/useCompactTurns'
import { useSession } from 'src/hooks/useSession'
import { compactProjectionCache } from 'src/lib/compactProjectionCache'
import type { Session, UserInputRequest } from 'src/types'

const session: Session = {
  id: 'selected',
  name: 'Selected',
  model: 'gpt-5.5',
  cwd: '/repo',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  messages: [],
  forks: [],
}

const page = {
  session_id: session.id,
  session,
  incarnation: 'process-1',
  render_revision: 4,
  events_watermark: 12,
  turns: [],
  page_cursor: { before_seq: null, has_older: false, revision: 'process-1:4' },
  pending_user_inputs: [],
}

afterEach(() => {
  compactProjectionCache.reset()
  vi.restoreAllMocks()
})

describe('single REST session-open lifecycle', () => {
  it('starts compact REST immediately for a direct deep link without waiting for the session list', async () => {
    const requests: string[] = []
    let resolveSessions!: (response: Response) => void
    const sessionsResponse = new Promise<Response>((resolve) => { resolveSessions = resolve })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      requests.push(url)
      if (url.includes('/turns?')) return new Response(JSON.stringify(page), { status: 200, headers: { 'Content-Type': 'application/json' } })
      return sessionsResponse
    }))
    const { result, unmount } = renderHook(() => {
      const sessions = useSession(undefined, session.id)
      const applySnapshot = useCallback(
        (snapshot: typeof page) => sessions.applyCompactSessionSnapshot(snapshot.session),
        [sessions.applyCompactSessionSnapshot],
      )
      const compact = useCompactTurns(sessions.selectedSessionId, applySnapshot)
      return { sessions, compact }
    })
    await waitFor(() => expect(result.current.compact.state?.status).toBe('ready'))
    expect(requests.filter((url) => url.includes('/turns?limit=5'))).toHaveLength(1)
    expect(result.current.sessions.sessionsLoaded).toBe(false)
    resolveSessions(new Response(JSON.stringify({ sessions: [session], has_more: false }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
    await waitFor(() => expect(result.current.sessions.sessionsLoaded).toBe(true))
    unmount()
  })
  it('coalesces StrictMode remount into exactly one initial request', async () => {
    let calls = 0
    const fiveTurnPage = {
      ...page,
      session: { ...session, forks: [] },
      turns: Array.from({ length: 5 }, (_, index) => ({
        id: `turn-${index}`, start_seq: index * 2 + 1, end_seq: index * 2 + 2,
        prompt: { id: `u-${index}`, content: `prompt ${index}` },
        assistant: {
          id: `a-${index}`, final_visible_text: `answer ${index}`, running: false,
          hydration_root: null, visible_text_groups: [], actionable_cards: [],
        },
      })),
    }
    vi.stubGlobal('fetch', vi.fn(async () => {
      calls += 1
      return new Response(JSON.stringify(fiveTurnPage), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    const wrapper = ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>
    const { result } = renderHook(() => useCompactTurns(session.id), { wrapper })
    await waitFor(() => expect(result.current.state?.status).toBe('ready'))
    expect(calls).toBe(1)
    expect(result.current.state?.turns).toHaveLength(5)
    expect(result.current.state?.session.forks).toEqual([])
  })

  it('applies consecutive WS revisions before React commits without resnapshotting', async () => {
    let calls = 0
    vi.stubGlobal('fetch', vi.fn(async () => {
      calls += 1
      return new Response(JSON.stringify(page), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    const { result } = renderHook(() => useCompactTurns(session.id))
    await waitFor(() => expect(result.current.state?.render_revision).toBe(4))
    act(() => {
      result.current.applyDelta({ incarnation: 'process-1', render_revision: 5, delta: { op: 'session_view', sid: session.id } })
      result.current.applyDelta({ incarnation: 'process-1', render_revision: 6, delta: { op: 'session_view', sid: session.id } })
    })
    await waitFor(() => expect(result.current.state?.render_revision).toBe(6))
    expect(calls).toBe(1)
  })
  it('selects from metadata without issuing the removed detail request', async () => {
    const requests: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      requests.push(url)
      return new Response(JSON.stringify({ sessions: [session], has_more: false }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }))

    const { result } = renderHook(() => useSession())
    await waitFor(() => expect(result.current.sessionsLoaded).toBe(true))
    await act(async () => { await result.current.selectSession(session.id) })

    expect(result.current.selectedSessionId).toBe(session.id)
    expect(result.current.currentSession?.id).toBe(session.id)
    expect(requests.some((url) => /\/api\/sessions\/selected(?:\?|$)/.test(url))).toBe(false)
    expect(requests.some((url) => url.includes('/user-input/pending'))).toBe(false)
  })

  it('uses one initial turns snapshot and stays REST-silent after ready', async () => {
    const requests: string[] = []
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push(String(input))
      expect(init?.cache).toBe('no-store')
      return new Response(JSON.stringify(page), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useCompactTurns(session.id))
    await waitFor(() => expect(result.current.state?.status).toBe('ready'))
    await act(async () => { await Promise.resolve() })

    expect(requests.filter((url) => url.includes('/turns?'))).toHaveLength(1)
    expect(requests.some((url) => /\/api\/sessions\/selected(?:\?|$)/.test(url))).toBe(false)
    expect(requests.some((url) => url.includes('/user-input/pending'))).toBe(false)
    expect(requests[0]).toContain('limit=5')
  })

  it('rejects malformed REST before it reaches rendering', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      ...page,
      turns: [{ id: 'broken', assistant: {} }],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })))
    const { result } = renderHook(() => useCompactTurns(session.id))
    await waitFor(() => expect(result.current.error).toBeInstanceOf(Error))
    expect(result.current.state).toBeNull()
  })

  it('discards malformed deltas and resnapshots', async () => {
    let calls = 0
    vi.stubGlobal('fetch', vi.fn(async () => {
      calls += 1
      return new Response(JSON.stringify(page), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    const { result } = renderHook(() => useCompactTurns(session.id))
    await waitFor(() => expect(result.current.state?.status).toBe('ready'))
    act(() => result.current.applyDelta({
      incarnation: 'process-1', render_revision: 5,
      delta: { op: 'replace_turn', sid: session.id, turn_id: 'broken', turn: {} } as never,
    }))
    await waitFor(() => {
      expect(calls).toBe(2)
      expect(result.current.state?.status).toBe('ready')
    })
  })

  it('permits REST only for paging and explicit resnapshot after ready', async () => {
    const requests: string[] = []
    const paged = {
      ...page,
      turns: [],
      page_cursor: { before_seq: 10, has_older: true, revision: 'process-1:4' },
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      requests.push(url)
      return new Response(JSON.stringify(requests.length === 1 ? paged : page), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }))

    const { result } = renderHook(() => useCompactTurns(session.id))
    await waitFor(() => expect(result.current.state?.status).toBe('ready'))
    await act(async () => { await result.current.loadOlder() })
    await act(async () => { await result.current.snapshot() })

    expect(requests).toHaveLength(3)
    expect(requests[0]).toContain('/turns?limit=')
    expect(requests[1]).toContain('before_seq=10')
    expect(requests[2]).not.toContain('before_seq=')
  })

  it('atomically replaces pending input state from the WS snapshot owner', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      ...page,
      pending_user_inputs: [{ request_id: 'stale', app_session_id: session.id }],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })))
    const { result } = renderHook(() => useCompactTurns(session.id))
    await waitFor(() => expect(result.current.state?.status).toBe('ready'))
    const fresh = [{ request_id: 'fresh', app_session_id: session.id }] as UserInputRequest[]

    act(() => result.current.replacePendingUserInputs(session.id, 2, fresh))
    expect(result.current.state?.pending_user_inputs).toBe(fresh)
    act(() => result.current.replacePendingUserInputs(session.id, 1, []))
    expect(result.current.state?.pending_user_inputs).toBe(fresh)
    act(() => result.current.replacePendingUserInputs('other-session', 3, []))
    expect(result.current.state?.pending_user_inputs).toBe(fresh)
  })

  it('refreshes the REST cursor after a revision gap before continuing', async () => {
    const requests: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      requests.push(String(input))
      return new Response(JSON.stringify({
        ...page,
        incarnation: requests.length === 1 ? 'process-1' : 'process-2',
        render_revision: requests.length === 1 ? 4 : 0,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
    const { result } = renderHook(() => useCompactTurns(session.id))
    await waitFor(() => expect(result.current.state?.render_revision).toBe(4))

    act(() => result.current.applyDelta({
      incarnation: 'process-1', render_revision: 6,
      delta: { op: 'session_view', sid: session.id },
    }))
    await waitFor(() => expect(result.current.state?.incarnation).toBe('process-2'))
    expect(requests.filter((url) => url.includes('/turns?'))).toHaveLength(2)
  })
})

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

    // Click A — REST A is parked.
    await act(async () => {
      void result.current.selectSession("a");
      await Promise.resolve();
    });
    expect(result.current.currentSession?.id).toBe("a");

    // Click B — REST B is parked. Optimistic state is now B.
    await act(async () => {
      void result.current.selectSession("b");
      await Promise.resolve();
    });
    expect(result.current.currentSession?.id).toBe("b");

    // Resolve REST A FIRST (out of order). The stale-request guard
    // (selectRequestIdRef) must drop it — currentSession stays on B.
    const canonicalA: Session = { ...a, messages: [] };
    await act(async () => {
      gate!.resolve(SESSION_FETCH, canonicalA);
      await Promise.resolve();
    });
    expect(result.current.currentSession?.id).toBe("b");

    // Resolve REST B — canonical state lands.
    const canonicalB: Session = { ...b, messages: [] };
    await act(async () => {
      gate!.resolve(SESSION_FETCH, canonicalB);
      await Promise.resolve();
    });
    expect(result.current.currentSession?.id).toBe("b");
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

  it("reopens a previously loaded session from the in-memory tree cache", async () => {
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
    await act(async () => {
      gate!.resolve(SESSION_FETCH, {
        ...a,
        messages: [
          {
            id: "a-msg",
            role: "user",
            content: "cached prompt",
            events: [],
            timestamp: new Date().toISOString(),
            isStreaming: false,
            seq: 0,
          },
        ],
      });
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(result.current.currentSession?.messages?.[0]?.content).toBe("cached prompt");
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
    expect(result.current.currentSession?.messages?.[0]?.content).toBe("cached prompt");
    expect(result.current.wsTargetSessionId).toBeNull();
    expect(gate.urls.filter((u) => SESSION_FETCH.test(u))).toHaveLength(fetchCountBeforeReopen + 1);

    await act(async () => {
      gate!.resolve(SESSION_FETCH, {
        ...a,
        messages: [
          {
            id: "a-msg-new",
            role: "user",
            content: "authoritative prompt",
            events: [],
            timestamp: new Date().toISOString(),
            isStreaming: false,
            seq: 1,
          },
        ],
      });
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(result.current.currentSession?.messages?.[0]?.content).toBe("authoritative prompt");
      expect(result.current.wsTargetSessionId).toBe("a");
    });
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
