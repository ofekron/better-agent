import { useCallback, useEffect, useRef, useSyncExternalStore } from 'react'
import { compactProjectionCache } from 'src/lib/compactProjectionCache'
import type { CompactRenderDelta, CompactTurnPage } from 'src/lib/compactTurns'
import type { UserInputRequest } from 'src/types'

export function useCompactTurns(sessionId: string | null, onSnapshot?: (page: CompactTurnPage) => void) {
  useSyncExternalStore(compactProjectionCache.subscribe, compactProjectionCache.getVersion)
  const onSnapshotRef = useRef(onSnapshot)
  onSnapshotRef.current = onSnapshot

  useEffect(() => {
    compactProjectionCache.setActive(sessionId)
  }, [sessionId])

  const view = compactProjectionCache.view(sessionId)
  useEffect(() => {
    if (view.state) onSnapshotRef.current?.(view.state)
  }, [view.state])

  const snapshot = useCallback(async () => {
    if (!sessionId) return
    const page = await compactProjectionCache.snapshot(sessionId, true)
    if (page) onSnapshotRef.current?.(page)
  }, [sessionId])

  const loadOlder = useCallback(async () => {
    if (sessionId) await compactProjectionCache.loadOlder(sessionId)
  }, [sessionId])

  const applyDelta = useCallback((envelope: { app_session_id?: string; incarnation: string; render_revision: number; delta: CompactRenderDelta }) => {
    const owner = envelope.app_session_id ?? sessionId
    if (owner) compactProjectionCache.applyDelta({ ...envelope, app_session_id: owner })
  }, [sessionId])

  const replacePendingUserInputs = useCallback((owner: string, revision: number, requests: UserInputRequest[]) => {
    compactProjectionCache.replacePending(owner, revision, requests)
  }, [])
  const getCursor = useCallback((owner: string) => compactProjectionCache.cursor(owner), [])
  const resnapshot = useCallback((owner: string) => compactProjectionCache.resnapshot(owner), [])
  const remove = useCallback((owner: string) => compactProjectionCache.remove(owner), [])

  return {
    ...view,
    snapshot,
    loadOlder,
    applyDelta,
    replacePendingUserInputs,
    getCursor,
    warmSessionIds: compactProjectionCache.warmIds(),
    resnapshot,
    remove,
  }
}
