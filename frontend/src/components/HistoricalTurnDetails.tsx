import { useMemo } from 'react'
import type { CompactManifest } from 'src/lib/compactTurns'
import { createHistoricalChildrenClient, toHistoricalManifest } from 'src/lib/historicalChildrenClient'
import { HistoricalHydrationStore } from 'src/lib/historicalHydrationStore'
import type { WSEvent, WorkerPanel } from 'src/types'
import { HistoricalNodeTree } from './HistoricalNodeTree'
import { MessageBubble } from './MessageBubble'

type Props = {
  sessionId: string
  messageId: string
  manifest: CompactManifest
}

function isWorkerPanel(value: unknown): value is WorkerPanel {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  return 'delegation_id' in value
    && typeof value.delegation_id === 'string'
    && 'worker_session_id' in value
    && typeof value.worker_session_id === 'string'
    && 'worker_description' in value
    && typeof value.worker_description === 'string'
    && 'is_new' in value
    && typeof value.is_new === 'boolean'
    && 'instructions_preview' in value
    && typeof value.instructions_preview === 'string'
    && 'events' in value
    && Array.isArray(value.events)
}

function isWsEvent(value: unknown): value is WSEvent {
  return !!value
    && typeof value === 'object'
    && !Array.isArray(value)
    && 'type' in value
    && typeof value.type === 'string'
    && 'data' in value
    && !!value.data
    && typeof value.data === 'object'
    && !Array.isArray(value.data)
}

export function HistoricalTurnDetails({ sessionId, messageId, manifest }: Props) {
  const store = useMemo(
    () => new HistoricalHydrationStore(createHistoricalChildrenClient(messageId)),
    [messageId],
  )
  const root = toHistoricalManifest(sessionId, manifest)

  return (
    <HistoricalNodeTree
      store={store}
      manifest={root}
      renderNode={({ manifest: node, expanded, expandable, toggleExpanded }) => {
        const payload = isWsEvent(node.renderPayload) ? node.renderPayload : undefined
        const worker = node.nodeId.startsWith('worker-') && isWorkerPanel(node.renderPayload)
          ? node.renderPayload
          : undefined
        return (
          <div className="historical-node" data-historical-node={node.nodeId}>
            {expandable && (
              <button
                type="button"
                className="raw-toggle"
                aria-label={node.summary}
                aria-expanded={expanded}
                onClick={toggleExpanded}
              >
                {expanded ? '−' : '+'}
              </button>
            )}
            {payload && (
              <MessageBubble
                message={{
                  id: `${messageId}:${node.nodeId}`,
                  role: 'assistant',
                  content: '',
                  events: worker ? [] : [payload],
                  workers: worker ? [worker] : [],
                  isStreaming: false,
                }}
                sessionId={sessionId}
              />
            )}
          </div>
        )
      }}
    />
  )
}
