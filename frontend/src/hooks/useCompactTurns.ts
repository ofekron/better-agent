import { useCallback, useEffect, useRef, useState } from 'react'
import { API } from 'src/api'
import {
  applyCompactRenderDelta,
  mergeOlderCompactTurns,
  type CompactRenderDelta,
  type CompactTurnPage,
  type CompactTurnsState,
  parseCompactTurnPage,
} from 'src/lib/compactTurns'
import type { UserInputRequest } from 'src/types'

const PAGE_SIZE = 5
const inFlightPages = new Map<string, Promise<CompactTurnPage>>()

async function fetchPage(sessionId: string, beforeSeq?: number | null): Promise<CompactTurnPage> {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE) })
  if (beforeSeq !== null && beforeSeq !== undefined) params.set('before_seq', String(beforeSeq))
  const response = await fetch(`${API}/api/sessions/${encodeURIComponent(sessionId)}/turns?${params}`, { credentials: 'include', cache: 'no-store' })
  if (!response.ok) throw new Error(`Compact turns request failed: ${response.status}`)
  return parseCompactTurnPage(await response.json())
}

function fetchPageOnce(sessionId: string): Promise<CompactTurnPage> {
  const existing = inFlightPages.get(sessionId)
  if (existing) return existing
  const request = fetchPage(sessionId).finally(() => {
    if (inFlightPages.get(sessionId) === request) inFlightPages.delete(sessionId)
  })
  inFlightPages.set(sessionId, request)
  return request
}

export function useCompactTurns(
  sessionId: string | null,
  onSnapshot?: (page: CompactTurnPage) => void,
) {
  const [state, setState] = useState<CompactTurnsState | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<unknown>()
  const generation = useRef(0)
  const stateRef = useRef(state)
  const onSnapshotRef = useRef(onSnapshot)
  stateRef.current = state
  onSnapshotRef.current = onSnapshot

  const snapshot = useCallback(async () => {
    if (!sessionId) return
    const requestGeneration = ++generation.current
    setLoading(true)
    try {
      const page = await fetchPageOnce(sessionId)
      if (generation.current !== requestGeneration) return
      const next = { ...page, status: 'ready' as const }
      stateRef.current = next
      setState(next)
      onSnapshotRef.current?.(page)
      setError(undefined)
    } catch (cause) {
      if (generation.current === requestGeneration) {
        stateRef.current = null
        setState(null)
        setError(cause)
      }
    } finally {
      if (generation.current === requestGeneration) setLoading(false)
    }
  }, [sessionId])

  useEffect(() => {
    setState(null)
    setError(undefined)
    if (sessionId) void snapshot()
    return () => { generation.current += 1 }
  }, [sessionId, snapshot])

  const loadOlder = useCallback(async () => {
    const current = stateRef.current
    if (!current?.page_cursor.has_older || current.page_cursor.before_seq === null) return
    const page = await fetchPage(current.session_id, current.page_cursor.before_seq)
    const latest = stateRef.current
    if (!latest) return
    const next = mergeOlderCompactTurns(latest, page)
    stateRef.current = next
    setState(next)
  }, [])

  const applyDelta = useCallback((envelope: { incarnation: string; render_revision: number; delta: CompactRenderDelta }) => {
    const current = stateRef.current
    if (!current) return
    try {
      const next = applyCompactRenderDelta(current, envelope)
      stateRef.current = next
      setState(next)
    } catch {
      stateRef.current = null
      setState(null)
      void snapshot()
    }
  }, [snapshot])

  const replacePendingUserInputs = useCallback((ownerSessionId: string, revision: number, requests: UserInputRequest[]) => {
    const current = stateRef.current
    if (!current || current.session_id !== ownerSessionId || revision < (current.pending_user_inputs_revision ?? -1)) return
    const next = { ...current, pending_user_inputs: requests, pending_user_inputs_revision: revision }
    stateRef.current = next
    setState(next)
  }, [])

  return { state, loading, error, snapshot, loadOlder, applyDelta, replacePendingUserInputs }
}
