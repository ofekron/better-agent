import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { useHistoricalNodeHydration } from 'src/hooks/useHistoricalNodeHydration'
import type {
  HistoricalHydrationState,
  HistoricalNodeManifest,
} from 'src/lib/historicalHydrationStore'
import { HistoricalHydrationStore } from 'src/lib/historicalHydrationStore'

export type HistoricalNodeRendererProps = HistoricalHydrationState & {
  expanded: boolean
  expandable: boolean
  toggleExpanded: () => void
}

export type HistoricalNodeTreeProps = {
  store: HistoricalHydrationStore
  manifest: HistoricalNodeManifest
  renderNode: (props: HistoricalNodeRendererProps) => ReactNode
}

export function HistoricalNodeTree({ store, manifest, renderNode }: HistoricalNodeTreeProps) {
  const [expanded, setExpanded] = useState(false)
  const hydration = useHistoricalNodeHydration(store, manifest)

  useEffect(() => {
    if (hydration.manifest.revision === manifest.revision) return
    store.invalidate(manifest, manifest.revision)
  }, [store, manifest, hydration.manifest.revision])

  const toggleExpanded = useCallback(() => {
    if (expanded) hydration.collapse()
    else void hydration.expand()
    setExpanded(!expanded)
  }, [expanded, hydration])

  return (
    <>
      {renderNode({ ...hydration, expanded, expandable: manifest.childCount > 0, toggleExpanded })}
      {expanded && hydration.status === 'ready' && hydration.children.map((child) => (
        <HistoricalNodeTree
          key={`${child.sessionId}:${child.nodeId}`}
          store={store}
          manifest={child}
          renderNode={renderNode}
        />
      ))}
    </>
  )
}
