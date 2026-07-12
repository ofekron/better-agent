import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useCompactTurns } from 'src/hooks/useCompactTurns'
import { useSession } from 'src/hooks/useSession'
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
  turns: [],
  page_cursor: { before_seq: null, has_older: false, revision: 'process-1:4' },
  pending_user_inputs: [],
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('single REST session-open lifecycle', () => {
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
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      requests.push(String(input))
      return new Response(JSON.stringify(page), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }))

    const { result } = renderHook(() => useCompactTurns(session.id))
    await waitFor(() => expect(result.current.state?.status).toBe('ready'))
    await act(async () => { await Promise.resolve() })

    expect(requests.filter((url) => url.includes('/turns?'))).toHaveLength(1)
    expect(requests.some((url) => /\/api\/sessions\/selected(?:\?|$)/.test(url))).toBe(false)
    expect(requests.some((url) => url.includes('/user-input/pending'))).toBe(false)
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
