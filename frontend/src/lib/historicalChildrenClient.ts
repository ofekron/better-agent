import { API } from 'src/api'
import type {
  HistoricalChildrenClient,
  HistoricalNodeManifest,
} from 'src/lib/historicalHydrationStore'

export type RawHistoricalManifest = {
  id: string
  type: string
  revision: string
  direct_child_count: number
  display_summary: string
}

type BackendChildrenResponse = {
  session_id: string
  message_id: string
  parent_id: string
  revision: string
  parent: RawHistoricalManifest
  children: Array<RawHistoricalManifest & { render_payload: unknown }>
  next_cursor?: string | null
  has_more?: boolean
}

export function toHistoricalManifest(sessionId: string, manifest: RawHistoricalManifest): HistoricalNodeManifest {
  return {
    sessionId,
    nodeId: manifest.id,
    revision: manifest.revision,
    childCount: manifest.direct_child_count,
    summary: manifest.display_summary,
  }
}

export function createHistoricalChildrenClient(messageId: string): HistoricalChildrenClient {
  return async (manifest, signal, cursor) => {
    const params = new URLSearchParams({ parent_id: manifest.nodeId, revision: manifest.revision })
    if (cursor) params.set('cursor', cursor)
    const response = await fetch(
      `${API}/api/sessions/${encodeURIComponent(manifest.sessionId)}/messages/${encodeURIComponent(messageId)}/children?${params}`,
      { credentials: 'include', signal },
    )
    if (!response.ok) throw new Error(`Historical children request failed: ${response.status}`)
    const payload = await response.json() as BackendChildrenResponse
    if (
      payload.session_id !== manifest.sessionId
      || payload.message_id !== messageId
      || payload.parent_id !== manifest.nodeId
      || payload.revision !== manifest.revision
    ) throw new Error('Historical children response identity mismatch')
    return {
      parent: toHistoricalManifest(manifest.sessionId, payload.parent),
      children: payload.children.map((child) => ({
        ...toHistoricalManifest(manifest.sessionId, child),
        renderPayload: child.render_payload,
      })),
      nextCursor: payload.next_cursor ?? null,
      hasMore: payload.has_more ?? false,
    }
  }
}
