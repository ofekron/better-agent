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
