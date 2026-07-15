import { useEffect, useLayoutEffect, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { createHistoricalChildrenClient, toHistoricalManifest, type RawHistoricalManifest } from 'src/lib/historicalChildrenClient'
import { HistoricalHydrationStore, type HistoricalNodeManifest } from 'src/lib/historicalHydrationStore'
import { useHistoricalNodeHydration } from 'src/hooks/useHistoricalNodeHydration'
import type { WSEvent, WorkerPanel } from 'src/types'
import { HistoricalNodeTree, type HistoricalNodeRendererProps } from './HistoricalNodeTree'
import { CanonicalHistoricalEventRow, CanonicalHistoricalWorkerRow } from './MessageBubble'

type Props = { sessionId: string; messageId: string; manifest: RawHistoricalManifest; active: boolean; onTerminal: () => void }

function isWorkerPanel(value: unknown): value is WorkerPanel {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const item = value as Record<string, unknown>
  return typeof item.delegation_id === 'string' && typeof item.worker_session_id === 'string'
    && typeof item.worker_description === 'string' && typeof item.is_new === 'boolean'
    && typeof item.instructions_preview === 'string' && Array.isArray(item.events)
}

function isWsEvent(value: unknown): value is WSEvent {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const item = value as Record<string, unknown>
  return typeof item.type === 'string' && !!item.data && typeof item.data === 'object' && !Array.isArray(item.data)
}

function HistoricalPayload({ node, sessionId, expanded, expandable, toggleExpanded, loading }: {
  node: HistoricalNodeManifest; sessionId: string; expanded: boolean; expandable: boolean; toggleExpanded: () => void; loading?: boolean
}) {
  const payload = isWsEvent(node.renderPayload) ? node.renderPayload : undefined
  const worker = isWorkerPanel(node.renderPayload) ? node.renderPayload : undefined
  return (
    <div className="historical-node" data-historical-node={node.nodeId}>
      {(payload || worker) ? (
        payload
          ? <CanonicalHistoricalEventRow event={payload} sessionId={sessionId} childControl={{ hasChildren: expandable, expanded, toggle: toggleExpanded, loading: !!loading, label: node.summary }} />
          : <CanonicalHistoricalWorkerRow worker={worker!} sessionId={sessionId} childControl={{ hasChildren: expandable, expanded, toggle: toggleExpanded, loading: !!loading, label: node.summary }} />
      ) : <div className="event-diagnostic" role="note">{node.summary}</div>}
    </div>
  )
}

export function HistoricalTurnDetails({ sessionId, messageId, manifest, active, onTerminal }: Props) {
  const { t } = useTranslation()
  const store = useMemo(() => new HistoricalHydrationStore(createHistoricalChildrenClient(messageId)), [messageId])
  const root = useMemo(
    () => toHistoricalManifest(sessionId, manifest),
    [sessionId, manifest],
  )
  const hydration = useHistoricalNodeHydration(store, root)
  const { collapse, expand } = hydration
  useEffect(() => {
    if (active) void expand()
    else collapse()
  }, [active, collapse, expand])
  useLayoutEffect(() => {
    if (!active || hydration.status === 'idle' || hydration.status === 'loading') return
    onTerminal()
  }, [active, hydration.status, onTerminal])
  if (!active) return null
  if (hydration.status === 'loading' || hydration.status === 'idle') return <div className="chat-loading-pulse historical-loading" aria-busy="true" />
  if (hydration.status === 'error' || hydration.status === 'stale') return (
    <div className="chat-load-error" role="alert">
      <span className="chat-load-error-text">{t('chat.sessionLoadFailed', { detail: hydration.error instanceof Error ? hydration.error.message : '' })}</span>
      <button type="button" className="chat-load-error-retry" onClick={() => void hydration.expand()}>{t('chat.sessionLoadRetry')}</button>
    </div>
  )
  if (hydration.children.length === 0) return null
  const renderNode = (props: HistoricalNodeRendererProps) => (
    <HistoricalPayload node={props.manifest} sessionId={sessionId} expanded={props.expanded} expandable={props.expandable} toggleExpanded={props.toggleExpanded} loading={props.status === 'loading'} />
  )
  return <div className="historical-turn-details">{hydration.children.map((child) => <HistoricalNodeTree key={`${child.sessionId}:${child.nodeId}`} store={store} manifest={child} renderNode={renderNode} />)}</div>
}
