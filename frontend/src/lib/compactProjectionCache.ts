import { API } from 'src/api'
import {
  applyCompactRenderDelta,
  mergeOlderCompactTurns,
  parseCompactTurnPage,
  type CompactRenderDelta,
  type CompactTurnPage,
  type CompactTurnsState,
} from 'src/lib/compactTurns'
import type { UserInputRequest } from 'src/types'

const PAGE_SIZE = 5
const MAX_ENTRIES = 20

type Entry = {
  state: CompactTurnsState | null
  loading: boolean
  error?: unknown
  stale: boolean
  access: number
  generation: number
  request?: Promise<CompactTurnPage>
  abort?: AbortController
  olderRequest?: Promise<void>
}

class CompactPageStaleError extends Error {}

export type CompactCacheView = {
  state: CompactTurnsState | null
  loading: boolean
  error?: unknown
}

export class CompactProjectionCache {
  private readonly entries = new Map<string, Entry>()
  private readonly listeners = new Set<() => void>()
  private activeId: string | null = null
  private access = 0
  private version = 0

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  getVersion = (): number => this.version

  view(sessionId: string | null): CompactCacheView {
    const entry = sessionId ? this.entries.get(sessionId) : undefined
    return entry ? { state: entry.state, loading: entry.loading, error: entry.error } : { state: null, loading: false }
  }

  ids(): string[] {
    return [...this.entries.keys()]
  }

  warmIds(): string[] {
    return this.ids().filter((id) => {
      const entry = this.entries.get(id)
      return id !== this.activeId && !!entry?.state && !entry.stale
    })
  }

  cursor(sessionId: string): { incarnation: string; renderRevision: number } | null {
    const state = this.entries.get(sessionId)?.state
    return state ? { incarnation: state.incarnation, renderRevision: state.render_revision } : null
  }

  setActive(sessionId: string | null): void {
    if (this.activeId === sessionId) return
    const previous = this.activeId
    this.activeId = sessionId
    if (previous) this.trimWarm(previous)
    if (sessionId) {
      const entry = this.entry(sessionId)
      entry.access = ++this.access
      if (!entry.state || entry.stale) void this.snapshot(sessionId)
    }
    this.evict()
    this.emit()
  }

  async snapshot(sessionId: string, force = false): Promise<CompactTurnPage | null> {
    const entry = this.entry(sessionId)
    if (entry.request && !force) return entry.request
    if (force) entry.abort?.abort()
    const generation = ++entry.generation
    const abort = new AbortController()
    entry.abort = abort
    entry.loading = true
    entry.error = undefined
    this.emit()
    const request = this.fetchPage(sessionId, null, abort.signal)
    entry.request = request
    try {
      const page = await request
      if (entry.generation !== generation || abort.signal.aborted || !this.entries.has(sessionId)) return null
      const next = { ...page, status: 'ready' as const }
      entry.state = next
      entry.stale = false
      entry.error = undefined
      if (sessionId !== this.activeId) this.trimWarm(sessionId)
      return page
    } catch (error) {
      if (entry.generation === generation && !abort.signal.aborted) {
        entry.state = null
        entry.stale = true
        entry.error = error
      }
      return null
    } finally {
      if (entry.generation === generation) {
        entry.loading = false
        entry.request = undefined
        entry.abort = undefined
        this.evict()
        this.emit()
      }
    }
  }

  async loadOlder(sessionId: string): Promise<void> {
    const entry = this.entries.get(sessionId)
    const current = entry?.state
    if (!entry || !current || sessionId !== this.activeId || !current.page_cursor.has_older || current.page_cursor.before_seq === null) return
    if (entry.olderRequest) return entry.olderRequest
    const request = (async () => {
      try {
        const page = await this.fetchPage(
          sessionId, current.page_cursor.before_seq, undefined, current.page_cursor.revision,
        )
        if (entry.state !== current || sessionId !== this.activeId) return
        entry.state = mergeOlderCompactTurns(current, page)
        this.emit()
      } catch (error) {
        if (!(error instanceof CompactPageStaleError)) throw error
        if (entry.state !== current || sessionId !== this.activeId) return
        entry.stale = true
        await this.snapshot(sessionId)
      }
    })()
    entry.olderRequest = request
    try {
      await request
    } finally {
      if (entry.olderRequest === request) entry.olderRequest = undefined
    }
  }

  applyDelta(envelope: { app_session_id: string; incarnation: string; render_revision: number; delta: CompactRenderDelta }): void {
    const entry = this.entries.get(envelope.app_session_id)
    if (!entry?.state) return
    if (envelope.delta.op === 'session_delete') {
      this.remove(envelope.app_session_id)
      return
    }
    try {
      entry.state = applyCompactRenderDelta(entry.state, envelope)
      if (envelope.app_session_id !== this.activeId) this.trimWarm(envelope.app_session_id)
      this.emit()
    } catch {
      entry.stale = true
      void this.snapshot(envelope.app_session_id)
    }
  }

  resnapshot(sessionId: string): Promise<CompactTurnPage | null> {
    const entry = this.entries.get(sessionId)
    if (!entry) return Promise.resolve(null)
    entry.stale = true
    return this.snapshot(sessionId)
  }

  replacePending(sessionId: string, revision: number, requests: UserInputRequest[]): void {
    const entry = this.entries.get(sessionId)
    const current = entry?.state
    if (!entry || !current || revision < (current.pending_user_inputs_revision ?? -1)) return
    entry.state = { ...current, pending_user_inputs: requests, pending_user_inputs_revision: revision }
    this.emit()
  }

  remove(sessionId: string): void {
    const entry = this.entries.get(sessionId)
    if (!entry) return
    entry.abort?.abort()
    this.entries.delete(sessionId)
    if (this.activeId === sessionId) this.activeId = null
    this.emit()
  }

  reset(): void {
    for (const entry of this.entries.values()) entry.abort?.abort()
    this.entries.clear()
    this.activeId = null
    this.emit()
  }

  private entry(sessionId: string): Entry {
    const existing = this.entries.get(sessionId)
    if (existing) return existing
    const created: Entry = { state: null, loading: false, stale: true, access: 0, generation: 0 }
    this.entries.set(sessionId, created)
    this.evict()
    return created
  }

  private trimWarm(sessionId: string): void {
    const entry = this.entries.get(sessionId)
    if (!entry?.state || entry.state.turns.length <= PAGE_SIZE) return
    const turns = entry.state.turns.slice(-PAGE_SIZE)
    const beforeSeq = turns[0]?.start_seq
    if (typeof beforeSeq !== 'number') throw new Error('Warm compact trim requires oldest retained sequence')
    entry.state = {
      ...entry.state,
      turns,
      page_cursor: { ...entry.state.page_cursor, before_seq: beforeSeq, has_older: true },
    }
  }

  private evict(): void {
    while (this.entries.size > MAX_ENTRIES) {
      const victim = [...this.entries.entries()]
        .filter(([id]) => id !== this.activeId)
        .sort((left, right) => left[1].access - right[1].access)[0]
      if (!victim) return
      victim[1].abort?.abort()
      this.entries.delete(victim[0])
    }
  }

  private async fetchPage(
    sessionId: string, beforeSeq: number | null, signal?: AbortSignal, cursorRevision?: string,
  ): Promise<CompactTurnPage> {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE) })
    if (beforeSeq !== null) {
      params.set('before_seq', String(beforeSeq))
      if (!cursorRevision) throw new Error('Compact load-more requires a snapshot revision')
      params.set('cursor_revision', cursorRevision)
    }
    const response = await fetch(`${API}/api/sessions/${encodeURIComponent(sessionId)}/turns?${params}`, {
      credentials: 'include', cache: 'no-store', signal,
    })
    if (!response.ok) {
      if (response.status === 409) {
        const body = await response.json().catch(() => null) as { detail?: { state?: string } } | null
        if (body?.detail?.state === 'compact_page_stale') throw new CompactPageStaleError()
      }
      throw new Error(`Compact turns request failed: ${response.status}`)
    }
    return parseCompactTurnPage(await response.json())
  }

  private emit(): void {
    this.version += 1
    for (const listener of this.listeners) listener()
  }
}

export const compactProjectionCache = new CompactProjectionCache()
