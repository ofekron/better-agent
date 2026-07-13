import { describe, expect, it, vi } from 'vitest'
import { HistoricalHydrationStore, type HistoricalNodeManifest } from 'src/lib/historicalHydrationStore'

const node = (nodeId: string, revision = 'r1'): HistoricalNodeManifest => ({ sessionId: 's1', nodeId, revision, childCount: 1, summary: nodeId })
const deferred = <T,>() => {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

describe('HistoricalHydrationStore', () => {
  it('fetches only on expansion, dedupes, and reuses an exact revision', async () => {
    const pending = deferred<{ parent: HistoricalNodeManifest; children: HistoricalNodeManifest[] }>()
    const client = vi.fn(() => pending.promise)
    const store = new HistoricalHydrationStore(client)
    expect(client).not.toHaveBeenCalled()
    const first = store.expand(node('parent'))
    const second = store.expand(node('parent'))
    expect(client).toHaveBeenCalledTimes(1)
    pending.resolve({ parent: node('parent'), children: [node('child')] })
    await expect(first).resolves.toMatchObject({ status: 'ready' })
    await second
    await store.expand(node('parent'))
    expect(client).toHaveBeenCalledTimes(1)
  })

  it('rejects grandchildren and event bodies in the wire schema', async () => {
    const store = new HistoricalHydrationStore(async () => ({
      parent: node('parent'),
      children: [{ ...node('child'), grandchildren: [] }],
    } as never))
    await store.expand(node('parent'))
    expect(store.get(node('parent')).status).toBe('error')
  })

  it('invalidates without fetching and rejects an old parent revision', async () => {
    const pending = deferred<{ parent: HistoricalNodeManifest; children: HistoricalNodeManifest[] }>()
    const client = vi.fn(() => pending.promise)
    const store = new HistoricalHydrationStore(client)
    const request = store.expand(node('parent'))
    store.invalidate(node('parent'), 'r2')
    expect(store.get(node('parent')).status).toBe('stale')
    expect(store.get(node('parent')).children).toEqual([])
    expect(client).toHaveBeenCalledTimes(1)
    pending.resolve({ parent: node('parent'), children: [node('child')] })
    await request
    expect(store.get(node('parent')).manifest.revision).toBe('r2')
    expect(store.get(node('parent')).status).toBe('stale')
  })

  it('never exposes children from an older revision after invalidation or failed refetch', async () => {
    const client = vi.fn()
      .mockResolvedValueOnce({ parent: node('parent'), children: [node('old-child')] })
      .mockRejectedValueOnce(new Error('failed refetch'))
    const store = new HistoricalHydrationStore(client)
    await store.expand(node('parent'))
    expect(store.get(node('parent')).children).toEqual([node('old-child')])

    store.invalidate(node('parent'), 'r2')
    expect(store.get(node('parent', 'r2')).children).toEqual([])
    const refetch = store.expand(node('parent', 'r2'))
    expect(store.get(node('parent', 'r2')).children).toEqual([])
    await refetch
    expect(store.get(node('parent', 'r2'))).toMatchObject({ status: 'error', children: [] })
  })

  it('aborts on collapse and removes tombstoned nodes', async () => {
    let signal: AbortSignal | undefined
    const store = new HistoricalHydrationStore((_manifest, receivedSignal) => {
      signal = receivedSignal
      return new Promise(() => undefined)
    })
    void store.expand(node('parent'))
    store.collapse(node('parent'))
    expect(signal?.aborted).toBe(true)
    expect(store.get(node('parent')).status).toBe('idle')
    store.invalidate(node('parent'), 'r2')
    store.tombstone(node('parent'))
    expect(store.get(node('parent')).status).toBe('idle')
  })

  it('rejects mismatched response identity and revision', async () => {
    const store = new HistoricalHydrationStore(async () => ({ parent: node('other', 'old'), children: [] }))
    await store.expand(node('parent', 'new'))
    expect(store.get(node('parent', 'new')).status).toBe('error')
  })

  it('loads more than one hundred direct children only by explicit cursor without duplicates', async () => {
    const children = Array.from({ length: 125 }, (_, index) => node(`child-${index}`))
    const client = vi.fn(async (_manifest: HistoricalNodeManifest, _signal: AbortSignal, cursor?: string) => {
      const offset = cursor ? Number(cursor) : 0
      const page = children.slice(offset, offset + 50)
      const next = offset + page.length
      return { parent: node('parent'), children: page, nextCursor: next < children.length ? String(next) : null, hasMore: next < children.length }
    })
    const store = new HistoricalHydrationStore(client)
    await store.expand(node('parent'))
    expect(store.get(node('parent')).children).toHaveLength(50)
    expect(client).toHaveBeenCalledTimes(1)
    await store.loadMore(node('parent'))
    expect(store.get(node('parent')).children).toHaveLength(100)
    await store.loadMore(node('parent'))
    const state = store.get(node('parent'))
    expect(state.children).toHaveLength(125)
    expect(new Set(state.children.map((child) => child.nodeId)).size).toBe(125)
    expect(state.hasMore).toBe(false)
    expect(client).toHaveBeenCalledTimes(3)
  })

  it('single-flights rapid load-more calls for the same cursor without duplicate children', async () => {
    const page = deferred<{ parent: HistoricalNodeManifest; children: HistoricalNodeManifest[]; nextCursor: null; hasMore: false }>()
    const client = vi.fn()
      .mockResolvedValueOnce({ parent: node('parent'), children: [node('first')], nextCursor: 'page-2', hasMore: true })
      .mockImplementationOnce(() => page.promise)
    const store = new HistoricalHydrationStore(client)
    await store.expand(node('parent'))

    const first = store.loadMore(node('parent'))
    const second = store.loadMore(node('parent'))
    expect(first).toBe(second)
    expect(client).toHaveBeenCalledTimes(2)
    expect(store.get(node('parent')).loadingMore).toBe(true)
    page.resolve({ parent: node('parent'), children: [node('first'), node('second')], nextCursor: null, hasMore: false })
    await Promise.all([first, second])

    expect(store.get(node('parent')).children.map((child) => child.nodeId)).toEqual(['first', 'second'])
    expect(store.get(node('parent')).loadingMore).toBe(false)
  })

  it('rejects a late pagination response after revision invalidation', async () => {
    const page = deferred<{ parent: HistoricalNodeManifest; children: HistoricalNodeManifest[]; nextCursor: null; hasMore: false }>()
    const client = vi.fn()
      .mockResolvedValueOnce({ parent: node('parent'), children: [node('old')], nextCursor: 'page-2', hasMore: true })
      .mockImplementationOnce(() => page.promise)
    const store = new HistoricalHydrationStore(client)
    await store.expand(node('parent'))
    const request = store.loadMore(node('parent'))

    store.invalidate(node('parent'), 'r2')
    page.resolve({ parent: node('parent'), children: [node('late')], nextCursor: null, hasMore: false })
    await request

    expect(store.get(node('parent', 'r2'))).toMatchObject({ status: 'stale', children: [], loadingMore: false })
  })

  it('retains ready children and retries the same cursor after a page failure', async () => {
    const client = vi.fn()
      .mockResolvedValueOnce({ parent: node('parent'), children: [node('first')], nextCursor: 'page-2', hasMore: true })
      .mockRejectedValueOnce(new Error('page unavailable'))
      .mockResolvedValueOnce({ parent: node('parent'), children: [node('second')], nextCursor: null, hasMore: false })
    const store = new HistoricalHydrationStore(client)
    await store.expand(node('parent'))

    await store.loadMore(node('parent'))
    expect(store.get(node('parent'))).toMatchObject({
      status: 'ready',
      children: [node('first')],
      nextCursor: 'page-2',
      hasMore: true,
      loadingMore: false,
      pageError: new Error('page unavailable'),
    })

    await store.loadMore(node('parent'))
    expect(client.mock.calls[1][2]).toBe('page-2')
    expect(client.mock.calls[2][2]).toBe('page-2')
    expect(store.get(node('parent'))).toMatchObject({
      status: 'ready', children: [node('first'), node('second')], hasMore: false, loadingMore: false,
    })
    expect(store.get(node('parent')).pageError).toBeUndefined()
  })
})
