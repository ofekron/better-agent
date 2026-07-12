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
})
