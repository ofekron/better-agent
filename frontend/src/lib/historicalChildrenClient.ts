import { API } from 'src/api'
import type { CompactManifest } from 'src/lib/compactTurns'
import type {
  HistoricalChildrenClient,
  HistoricalNodeManifest,
} from 'src/lib/historicalHydrationStore'

type BackendChildrenResponse = {
  session_id: string
  message_id: string
  parent_id: string
  revision: string
  parent: CompactManifest
  children: Array<CompactManifest & { render_payload: unknown }>
}

export function toHistoricalManifest(sessionId: string, manifest: CompactManifest): HistoricalNodeManifest {
  return {
    sessionId,
    nodeId: manifest.id,
    revision: manifest.revision,
    childCount: manifest.direct_child_count,
    summary: manifest.display_summary,
  }
}

export function createHistoricalChildrenClient(messageId: string): HistoricalChildrenClient {
  return async (manifest, signal) => {
    const params = new URLSearchParams({ parent_id: manifest.nodeId, revision: manifest.revision })
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
    }
  }
}
