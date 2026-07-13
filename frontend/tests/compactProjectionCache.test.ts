import { afterEach, describe, expect, it, vi } from 'vitest'
import { CompactProjectionCache } from 'src/lib/compactProjectionCache'
import { buildCompactSubscriptionModes } from 'src/lib/compactSubscriptionIntents'
import type { Session } from 'src/types'

const session = (id: string): Session => ({ id, name: id, model: 'gpt-5.5', cwd: '/repo', created_at: '2026-01-01', updated_at: '2026-01-01', messages: [], forks: [] })
const page = (id: string, revision = 1, turns = 1) => ({
  session_id: id, session: session(id), incarnation: 'process-1', render_revision: revision, events_watermark: 0,
  turns: Array.from({ length: turns }, (_, index) => ({
    id: `${id}-turn-${index}`, start_seq: index * 2 + 1, end_seq: index * 2 + 2,
    prompt: { id: `${id}-u-${index}`, content: `${index}` },
    assistant: { id: `${id}-a-${index}`, final_visible_text: `${index}`, running: false, hydration_root: null, visible_text_groups: [], actionable_cards: [] },
  })),
  page_cursor: { before_seq: turns > 1 ? 2 : null, has_older: turns > 1, revision: `process-1:${revision}` }, pending_user_inputs: [],
})
const idFrom = (input: RequestInfo | URL) => decodeURIComponent(String(input).match(/sessions\/([^/]+)\/turns/)?.[1] ?? '')

afterEach(() => vi.restoreAllMocks())

describe('CompactProjectionCache', () => {
  it('pins active, caps at 20, and evicts least-recent inactive', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(page(idFrom(input))), { status: 200 })))
    const cache = new CompactProjectionCache()
    for (let index = 0; index < 21; index += 1) { cache.setActive(`s${index}`); await cache.snapshot(`s${index}`) }
    expect(cache.ids()).toHaveLength(20)
    expect(cache.ids()).toContain('s20')
    expect(cache.ids()).not.toContain('s0')
  })

  it('aborts an evicted inactive request', async () => {
    const aborted: string[] = []
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const id = idFrom(input)
      if (id === 'slow') return new Promise<Response>((_resolve, reject) => init?.signal?.addEventListener('abort', () => { aborted.push(id); reject(new DOMException('Aborted', 'AbortError')) }))
      return Promise.resolve(new Response(JSON.stringify(page(id)), { status: 200 }))
    }))
    const cache = new CompactProjectionCache()
    cache.setActive('slow'); cache.setActive('s0')
    for (let index = 1; index < 20; index += 1) { cache.setActive(`s${index}`); await cache.snapshot(`s${index}`) }
    expect(aborted).toEqual(['slow'])
    expect(cache.ids()).not.toContain('slow')
  })

  it('applies warm deltas and coalesces one revision-gap resnapshot', async () => {
    const calls = new Map<string, number>()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const id = idFrom(input); calls.set(id, (calls.get(id) ?? 0) + 1)
      return new Response(JSON.stringify(page(id, id === 'warm' && calls.get(id) === 2 ? 4 : 1)), { status: 200 })
    }))
    const cache = new CompactProjectionCache()
    cache.setActive('warm')
    await vi.waitFor(() => expect(cache.cursor('warm')?.renderRevision).toBe(1))
    cache.setActive('active')
    cache.applyDelta({ app_session_id: 'warm', incarnation: 'process-1', render_revision: 2, delta: { op: 'session_view', sid: 'warm' } })
    expect(cache.cursor('warm')?.renderRevision).toBe(2)
    cache.applyDelta({ app_session_id: 'warm', incarnation: 'process-1', render_revision: 4, delta: { op: 'session_view', sid: 'warm' } })
    cache.applyDelta({ app_session_id: 'warm', incarnation: 'process-1', render_revision: 5, delta: { op: 'session_view', sid: 'warm' } })
    await vi.waitFor(() => expect(cache.cursor('warm')?.renderRevision).toBe(4))
    expect(calls.get('warm')).toBe(2)
  })

  it('trims loaded history to five turns on downgrade', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(page(idFrom(input), 1, 8)), { status: 200 })))
    const cache = new CompactProjectionCache()
    cache.setActive('s'); await cache.snapshot('s'); cache.setActive('other')
    expect(cache.view('s').state?.turns).toHaveLength(5)
  })

  it('pages from the retained warm boundary without holes or duplicates', async () => {
    const requested: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input); requested.push(url); const id = idFrom(input)
      if (id === 'warm' && url.includes('before_seq=7')) {
        const older = page('warm', 4, 3)
        older.page_cursor = { before_seq: null, has_older: false, revision: 'process-1:4' }
        return new Response(JSON.stringify(older), { status: 200 })
      }
      return new Response(JSON.stringify(page(id, 1, id === 'warm' ? 5 : 1)), { status: 200 })
    }))
    const cache = new CompactProjectionCache()
    cache.setActive('warm'); await vi.waitFor(() => expect(cache.cursor('warm')).not.toBeNull()); cache.setActive('active')
    for (let revision = 2; revision <= 4; revision += 1) {
      const base = page('warm', revision).turns[0]
      const start = revision * 2 + 7
      cache.applyDelta({
        app_session_id: 'warm', incarnation: 'process-1', render_revision: revision,
        delta: { op: 'replace_turn', sid: 'warm', turn_id: `warm-turn-${revision + 3}`, turn: { ...base, id: `warm-turn-${revision + 3}`, start_seq: start, end_seq: start + 1 } },
      })
    }
    expect(cache.view('warm').state?.page_cursor.before_seq).toBe(7)
    cache.setActive('warm')
    await cache.loadOlder('warm')
    expect(requested.some((url) => url.includes('before_seq=7'))).toBe(true)
    expect(cache.view('warm').state?.turns.map((turn) => turn.start_seq)).toEqual([1, 3, 5, 7, 9, 11, 13, 15])
  })

  it('fails closed and resnapshots when a warm trim boundary has no sequence', async () => {
    let warmCalls = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const id = idFrom(input)
      if (id === 'warm') warmCalls += 1
      return new Response(JSON.stringify(page(id, warmCalls === 2 ? 9 : 1, id === 'warm' ? 5 : 1)), { status: 200 })
    }))
    const cache = new CompactProjectionCache()
    cache.setActive('warm'); await vi.waitFor(() => expect(cache.cursor('warm')).not.toBeNull()); cache.setActive('active')
    const base = page('warm', 2).turns[0]
    for (let revision = 2; revision <= 6; revision += 1) {
      cache.applyDelta({
        app_session_id: 'warm', incarnation: 'process-1', render_revision: revision,
        delta: {
          op: 'replace_turn', sid: 'warm', turn_id: `new-${revision}`,
          turn: { ...base, id: `new-${revision}`, start_seq: revision === 2 ? null : revision * 2 + 7, end_seq: revision * 2 + 8 },
        },
      })
    }
    await vi.waitFor(() => expect(cache.cursor('warm')?.renderRevision).toBe(9))
    expect(warmCalls).toBe(2)
  })

  it('makes a rapid previous open subscribable when its cursor arrives late', async () => {
    const resolvers = new Map<string, (response: Response) => void>()
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => new Promise<Response>((resolve) => resolvers.set(idFrom(input), resolve))))
    const cache = new CompactProjectionCache()
    cache.setActive('a'); cache.setActive('b')
    resolvers.get('b')?.(new Response(JSON.stringify(page('b')), { status: 200 }))
    await vi.waitFor(() => expect(cache.cursor('b')).not.toBeNull())
    expect(cache.warmIds()).not.toContain('a')
    resolvers.get('a')?.(new Response(JSON.stringify(page('a')), { status: 200 }))
    await vi.waitFor(() => expect(cache.warmIds()).toContain('a'))
    expect(cache.cursor('a')).toEqual({ incarnation: 'process-1', renderRevision: 1 })
  })

  it('does not let background mutations refresh explicit-open LRU order', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(page(idFrom(input))), { status: 200 })))
    const cache = new CompactProjectionCache()
    for (let index = 0; index < 20; index += 1) { cache.setActive(`s${index}`); await vi.waitFor(() => expect(cache.cursor(`s${index}`)).not.toBeNull()) }
    for (let revision = 2; revision < 10; revision += 1) {
      cache.applyDelta({ app_session_id: 's0', incarnation: 'process-1', render_revision: revision, delta: { op: 'session_view', sid: 's0' } })
    }
    cache.replacePending('s0', 99, [])
    cache.setActive('s20')
    expect(cache.ids()).not.toContain('s0')
    expect(cache.ids()).toContain('s1')
  })

  it('caps every warm delta mutation at five turns', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(page(idFrom(input), 1, 5)), { status: 200 })))
    const cache = new CompactProjectionCache()
    cache.setActive('warm'); await vi.waitFor(() => expect(cache.cursor('warm')).not.toBeNull()); cache.setActive('active')
    for (let revision = 2; revision < 20; revision += 1) {
      const turn = page('warm', revision).turns[0]
      cache.applyDelta({ app_session_id: 'warm', incarnation: 'process-1', render_revision: revision, delta: { op: 'replace_turn', sid: 'warm', turn_id: `new-${revision}`, turn: { ...turn, id: `new-${revision}` } } })
      expect(cache.view('warm').state?.turns).toHaveLength(5)
    }
  })

  it('coalesces gaps independently and removes a deleted warm entry immediately', async () => {
    const calls = new Map<string, number>()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const id = idFrom(input); calls.set(id, (calls.get(id) ?? 0) + 1)
      return new Response(JSON.stringify(page(id, calls.get(id) === 2 ? 8 : 1)), { status: 200 })
    }))
    const cache = new CompactProjectionCache()
    cache.setActive('warm'); await vi.waitFor(() => expect(cache.cursor('warm')).not.toBeNull())
    cache.setActive('active'); await vi.waitFor(() => expect(cache.cursor('active')).not.toBeNull())
    for (const id of ['warm', 'active']) {
      cache.applyDelta({ app_session_id: id, incarnation: 'other', render_revision: 9, delta: { op: 'session_view', sid: id } })
      cache.applyDelta({ app_session_id: id, incarnation: 'other', render_revision: 10, delta: { op: 'session_view', sid: id } })
    }
    await vi.waitFor(() => expect(cache.cursor('warm')?.renderRevision).toBe(8))
    await vi.waitFor(() => expect(cache.cursor('active')?.renderRevision).toBe(8))
    expect(calls.get('warm')).toBe(2); expect(calls.get('active')).toBe(2)
    cache.applyDelta({ app_session_id: 'warm', incarnation: 'process-1', render_revision: 9, delta: { op: 'session_delete', sid: 'warm' } })
    expect(cache.ids()).not.toContain('warm')
    expect(cache.warmIds()).not.toContain('warm')
  })
})

it('keeps visible panes foreground and bounds warm roots to nineteen cache subscriptions', () => {
  const modes = buildCompactSubscriptionModes(
    'active',
    ['pane-1', 'pane-2', 'pane-3'],
    ['pane-1', ...Array.from({ length: 30 }, (_, index) => `warm-${index}`)],
  )
  expect([...modes].slice(0, 5)).toEqual([
    ['active', 'foreground'], ['pane-1', 'foreground'], ['pane-2', 'foreground'], ['pane-3', 'foreground'], ['warm-0', 'cache'],
  ])
  expect([...modes.values()].filter((mode) => mode === 'foreground')).toHaveLength(4)
  expect([...modes.values()].filter((mode) => mode === 'cache')).toHaveLength(19)
  expect(modes.get('pane-1')).toBe('foreground')
  expect(modes.get('warm-19')).toBeUndefined()
})
