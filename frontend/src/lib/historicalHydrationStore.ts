export type HistoricalNodeManifest = {
  sessionId: string
  nodeId: string
  revision: string
  childCount: number
  summary: string
  renderPayload?: unknown
}

export type HistoricalChildrenResponse = {
  parent: HistoricalNodeManifest
  children: HistoricalNodeManifest[]
}

export type HistoricalHydrationStatus = 'idle' | 'loading' | 'ready' | 'stale' | 'error'

export type HistoricalHydrationState = {
  status: HistoricalHydrationStatus
  manifest: HistoricalNodeManifest
  children: HistoricalNodeManifest[]
  error?: unknown
}

export type HistoricalChildrenClient = (
  manifest: HistoricalNodeManifest,
  signal: AbortSignal,
) => Promise<HistoricalChildrenResponse>

const manifestFields = new Set(['sessionId', 'nodeId', 'revision', 'childCount', 'summary', 'renderPayload'])

function isManifest(value: unknown): value is HistoricalNodeManifest {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const record = value as Record<string, unknown>
  if (Object.keys(record).some((key) => !manifestFields.has(key))) return false
  return typeof record.sessionId === 'string'
    && typeof record.nodeId === 'string'
    && typeof record.revision === 'string'
    && Number.isInteger(record.childCount)
    && (record.childCount as number) >= 0
    && typeof record.summary === 'string'
}

function assertResponse(value: unknown, requested: HistoricalNodeManifest): HistoricalChildrenResponse {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid historical children response')
  const record = value as Record<string, unknown>
  if (Object.keys(record).some((key) => key !== 'parent' && key !== 'children')) throw new Error('Historical response contains unsupported nested data')
  if (!isManifest(record.parent) || !Array.isArray(record.children) || !record.children.every(isManifest)) throw new Error('Invalid historical children response')
  if (record.parent.sessionId !== requested.sessionId || record.parent.nodeId !== requested.nodeId || record.parent.revision !== requested.revision) {
    throw new Error('Stale historical children response')
  }
  if (record.children.some((child) => child.sessionId !== requested.sessionId)) throw new Error('Historical child belongs to another session')
  return { parent: record.parent, children: record.children }
}

const keyOf = (manifest: Pick<HistoricalNodeManifest, 'sessionId' | 'nodeId'>) => `${manifest.sessionId}\u0000${manifest.nodeId}`

export class HistoricalHydrationStore {
  private readonly fetchChildren: HistoricalChildrenClient
  private states = new Map<string, HistoricalHydrationState>()
  private requests = new Map<string, { revision: string; controller: AbortController; promise: Promise<HistoricalHydrationState> }>()
  private listeners = new Map<string, Set<() => void>>()

  constructor(fetchChildren: HistoricalChildrenClient) {
    this.fetchChildren = fetchChildren
  }

  get(manifest: HistoricalNodeManifest): HistoricalHydrationState {
    const key = keyOf(manifest)
    const existing = this.states.get(key)
    if (existing) return existing
    const idle: HistoricalHydrationState = { status: 'idle', manifest, children: [] }
    this.states.set(key, idle)
    return idle
  }

  subscribe(manifest: HistoricalNodeManifest, listener: () => void): () => void {
    const key = keyOf(manifest)
    const listeners = this.listeners.get(key) ?? new Set()
    listeners.add(listener)
    this.listeners.set(key, listeners)
    return () => {
      listeners.delete(listener)
      if (!listeners.size) this.listeners.delete(key)
    }
  }

  expand(manifest: HistoricalNodeManifest): Promise<HistoricalHydrationState> {
    const key = keyOf(manifest)
    const cached = this.states.get(key)
    if (cached?.status === 'ready' && cached.manifest.revision === manifest.revision) return Promise.resolve(cached)
    const active = this.requests.get(key)
    if (active?.revision === manifest.revision) return active.promise
    active?.controller.abort()

    const controller = new AbortController()
    this.set(key, { status: 'loading', manifest, children: [] })
    const promise = this.fetchChildren(manifest, controller.signal)
      .then((response) => assertResponse(response, manifest))
      .then((response) => {
        if (controller.signal.aborted || this.requests.get(key)?.promise !== promise) return this.get(manifest)
        const state: HistoricalHydrationState = { status: 'ready', manifest: response.parent, children: response.children }
        this.set(key, state)
        return state
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || this.requests.get(key)?.promise !== promise) return this.get(manifest)
        const state: HistoricalHydrationState = { status: 'error', manifest, children: [], error }
        this.set(key, state)
        return state
      })
      .finally(() => {
        if (this.requests.get(key)?.promise === promise) this.requests.delete(key)
      })
    this.requests.set(key, { revision: manifest.revision, controller, promise })
    return promise
  }

  collapse(manifest: HistoricalNodeManifest): void {
    const key = keyOf(manifest)
    this.requests.get(key)?.controller.abort()
    this.requests.delete(key)
    const state = this.states.get(key)
    if (state?.status === 'loading') this.set(key, { ...state, status: state.children.length ? 'stale' : 'idle' })
  }

  invalidate(manifest: HistoricalNodeManifest, newRevision: string): void {
    const key = keyOf(manifest)
    this.requests.get(key)?.controller.abort()
    this.requests.delete(key)
    const current = this.states.get(key)
    this.set(key, {
      status: 'stale',
      manifest: { ...(current?.manifest ?? manifest), revision: newRevision },
      children: [],
    })
  }

  tombstone(manifest: Pick<HistoricalNodeManifest, 'sessionId' | 'nodeId'>): void {
    const key = keyOf(manifest)
    this.requests.get(key)?.controller.abort()
    this.requests.delete(key)
    this.states.delete(key)
    this.emit(key)
  }

  private set(key: string, state: HistoricalHydrationState): void {
    this.states.set(key, state)
    this.emit(key)
  }

  private emit(key: string): void {
    this.listeners.get(key)?.forEach((listener) => listener())
  }
}
