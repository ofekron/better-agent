import { useCallback, useEffect, useRef, useState } from 'react'
import { API } from 'src/api'
import {
  applyCompactRenderDelta,
  mergeOlderCompactTurns,
  type CompactRenderDelta,
  type CompactTurnPage,
  type CompactTurnsState,
} from 'src/lib/compactTurns'
import type { UserInputRequest } from 'src/types'

const PAGE_SIZE = 20

async function fetchPage(sessionId: string, beforeSeq?: number | null): Promise<CompactTurnPage> {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE) })
  if (beforeSeq !== null && beforeSeq !== undefined) params.set('before_seq', String(beforeSeq))
  const response = await fetch(`${API}/api/sessions/${encodeURIComponent(sessionId)}/turns?${params}`, { credentials: 'include' })
  if (!response.ok) throw new Error(`Compact turns request failed: ${response.status}`)
  return response.json() as Promise<CompactTurnPage>
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
  stateRef.current = state

  const snapshot = useCallback(async () => {
    if (!sessionId) return
    const requestGeneration = ++generation.current
    setLoading(true)
    try {
      const page = await fetchPage(sessionId)
      if (generation.current !== requestGeneration) return
      setState({ ...page, status: 'ready' })
      onSnapshot?.(page)
      setError(undefined)
    } catch (cause) {
      if (generation.current === requestGeneration) setError(cause)
    } finally {
      if (generation.current === requestGeneration) setLoading(false)
    }
  }, [sessionId, onSnapshot])

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
    setState((latest) => latest ? mergeOlderCompactTurns(latest, page) : latest)
  }, [])

  const applyDelta = useCallback((envelope: { incarnation: string; render_revision: number; delta: CompactRenderDelta }) => {
    const current = stateRef.current
    if (!current) return
    try {
      setState(applyCompactRenderDelta(current, envelope))
    } catch {
      void snapshot()
    }
  }, [snapshot])

  const replacePendingUserInputs = useCallback((ownerSessionId: string, revision: number, requests: UserInputRequest[]) => {
    setState((current) => current?.session_id === ownerSessionId
      && revision >= (current.pending_user_inputs_revision ?? -1)
      ? { ...current, pending_user_inputs: requests, pending_user_inputs_revision: revision }
      : current)
  }, [])

  return { state, loading, error, snapshot, loadOlder, applyDelta, replacePendingUserInputs }
}
