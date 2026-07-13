import { useEffect, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import type { CompactManifest } from 'src/lib/compactTurns'
import { createHistoricalChildrenClient, toHistoricalManifest } from 'src/lib/historicalChildrenClient'
import { HistoricalHydrationStore, type HistoricalNodeManifest } from 'src/lib/historicalHydrationStore'
import { useHistoricalNodeHydration } from 'src/hooks/useHistoricalNodeHydration'
import type { WSEvent, WorkerPanel } from 'src/types'
import { HistoricalNodeTree, type HistoricalNodeRendererProps } from './HistoricalNodeTree'
import { MessageBubble } from './MessageBubble'

type Props = { sessionId: string; messageId: string; manifest: CompactManifest; active: boolean }

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

function HistoricalPayload({ node, messageId, sessionId, expanded, expandable, toggleExpanded }: {
  node: HistoricalNodeManifest; messageId: string; sessionId: string; expanded: boolean; expandable: boolean; toggleExpanded: () => void
}) {
  const payload = isWsEvent(node.renderPayload) ? node.renderPayload : undefined
  const worker = isWorkerPanel(node.renderPayload) ? node.renderPayload : undefined
  return (
    <div className="historical-node" data-historical-node={node.nodeId}>
      {expandable && <button type="button" className="raw-toggle" aria-label={node.summary} aria-expanded={expanded} onClick={toggleExpanded}>{expanded ? '−' : '+'}</button>}
      {(payload || worker) ? (
        <MessageBubble
          message={{ id: `${messageId}:${node.nodeId}`, role: 'assistant', content: '', events: payload ? [payload] : [], workers: worker ? [worker] : [], isStreaming: false }}
          sessionId={sessionId}
        />
      ) : <div className="event-diagnostic" role="note">{node.summary}</div>}
    </div>
  )
}

export function HistoricalTurnDetails({ sessionId, messageId, manifest, active }: Props) {
  const { t } = useTranslation()
  const store = useMemo(() => new HistoricalHydrationStore(createHistoricalChildrenClient(messageId)), [messageId])
  const root = useMemo(
    () => toHistoricalManifest(sessionId, manifest),
    [sessionId, manifest.id, manifest.revision, manifest.direct_child_count, manifest.display_summary],
  )
  const hydration = useHistoricalNodeHydration(store, root)
  const { collapse, expand } = hydration
  useEffect(() => {
    if (active) void expand()
    else collapse()
  }, [active, collapse, expand])
  if (!active) return null
  if (hydration.status === 'loading' || hydration.status === 'idle') return <div className="chat-loading-pulse historical-loading" aria-busy="true" />
  if (hydration.status === 'error' || hydration.status === 'stale') return (
    <div className="chat-load-error" role="alert">
      <span className="chat-load-error-text">{t('chat.sessionLoadFailed', { detail: hydration.error instanceof Error ? hydration.error.message : '' })}</span>
      <button type="button" className="chat-load-error-retry" onClick={() => void hydration.expand()}>{t('chat.sessionLoadRetry')}</button>
    </div>
  )
  if (hydration.children.length === 0) return <div className="event-diagnostic" role="status">{root.summary || '—'}</div>
  const renderNode = (props: HistoricalNodeRendererProps) => (
    <HistoricalPayload node={props.manifest} messageId={messageId} sessionId={sessionId} expanded={props.expanded} expandable={props.expandable} toggleExpanded={props.toggleExpanded} />
  )
  return <div className="historical-turn-details">{hydration.children.map((child) => <HistoricalNodeTree key={`${child.sessionId}:${child.nodeId}`} store={store} manifest={child} renderNode={renderNode} />)}</div>
}
