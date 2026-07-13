import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
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
  const { t } = useTranslation()
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
      {expanded && (hydration.status === 'loading' || hydration.status === 'idle') && <div className="chat-loading-pulse historical-loading" aria-busy="true" />}
      {expanded && (hydration.status === 'error' || hydration.status === 'stale') && (
        <div className="chat-load-error" role="alert">
          <span className="chat-load-error-text">{t('chat.sessionLoadFailed', { detail: hydration.error instanceof Error ? hydration.error.message : '' })}</span>
          <button type="button" className="chat-load-error-retry" onClick={() => void hydration.expand()}>{t('chat.sessionLoadRetry')}</button>
        </div>
      )}
      {expanded && hydration.status === 'ready' && hydration.children.length === 0 && <div className="event-diagnostic" role="status">{manifest.summary || '—'}</div>}
      {expanded && hydration.status === 'ready' && hydration.children.map((child) => (
          <HistoricalNodeTree
            key={`${child.sessionId}:${child.nodeId}`}
            store={store}
            manifest={child}
            renderNode={renderNode}
          />
        ))}
      {expanded && hydration.status === 'ready' && hydration.pageError !== undefined && (
        <div className="chat-load-error historical-page-error" role="alert">
          <span className="chat-load-error-text">{t('chat.pageLoadFailed', { detail: hydration.pageError instanceof Error ? hydration.pageError.message : '' })}</span>
          <button type="button" className="chat-load-error-retry" onClick={() => void hydration.loadMore()}>{t('chat.pageLoadRetry')}</button>
        </div>
      )}
      {expanded && hydration.status === 'ready' && hydration.hasMore && (
        <button
          type="button"
          className="load-older-link historical-load-more"
          disabled={hydration.loadingMore}
          aria-busy={hydration.loadingMore}
          onClick={() => void hydration.loadMore()}
        >{t('chat.loadOlderMessages')}</button>
      )}
    </>
  )
}
