import { useCallback, useSyncExternalStore } from 'react'
import type { HistoricalNodeManifest } from 'src/lib/historicalHydrationStore'
import { HistoricalHydrationStore } from 'src/lib/historicalHydrationStore'

export function useHistoricalNodeHydration(store: HistoricalHydrationStore, manifest: HistoricalNodeManifest) {
  const subscribe = useCallback((listener: () => void) => store.subscribe(manifest, listener), [store, manifest.sessionId, manifest.nodeId])
  const snapshot = useCallback(() => store.get(manifest), [store, manifest])
  const state = useSyncExternalStore(subscribe, snapshot, snapshot)

  return {
    ...state,
    expand: useCallback(() => store.expand(manifest), [store, manifest]),
    collapse: useCallback(() => store.collapse(manifest), [store, manifest]),
    loadMore: useCallback(() => store.loadMore(manifest), [store, manifest]),
  }
}
