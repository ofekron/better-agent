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
  nextCursor?: string | null
  hasMore?: boolean
}

export type HistoricalHydrationStatus = 'idle' | 'loading' | 'ready' | 'stale' | 'error'

export type HistoricalHydrationState = {
  status: HistoricalHydrationStatus
  manifest: HistoricalNodeManifest
  children: HistoricalNodeManifest[]
  nextCursor: string | null
  hasMore: boolean
  loadingMore: boolean
  pageError?: unknown
  error?: unknown
}

export type HistoricalChildrenClient = (
  manifest: HistoricalNodeManifest,
  signal: AbortSignal,
  cursor?: string,
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
  if (Object.keys(record).some((key) => !['parent', 'children', 'nextCursor', 'hasMore'].includes(key))) throw new Error('Historical response contains unsupported nested data')
  if (!isManifest(record.parent) || !Array.isArray(record.children) || !record.children.every(isManifest)) throw new Error('Invalid historical children response')
  if (record.parent.sessionId !== requested.sessionId || record.parent.nodeId !== requested.nodeId || record.parent.revision !== requested.revision) {
    throw new Error('Stale historical children response')
  }
  if (record.children.some((child) => child.sessionId !== requested.sessionId)) throw new Error('Historical child belongs to another session')
  if (!(record.nextCursor === undefined || record.nextCursor === null || typeof record.nextCursor === 'string') || !(record.hasMore === undefined || typeof record.hasMore === 'boolean')) throw new Error('Invalid historical pagination')
  return { parent: record.parent, children: record.children, nextCursor: record.nextCursor as string | null | undefined, hasMore: record.hasMore as boolean | undefined }
}

const keyOf = (manifest: Pick<HistoricalNodeManifest, 'sessionId' | 'nodeId'>) => `${manifest.sessionId}\u0000${manifest.nodeId}`

export class HistoricalHydrationStore {
  private readonly fetchChildren: HistoricalChildrenClient
  private states = new Map<string, HistoricalHydrationState>()
  private requests = new Map<string, { revision: string; controller: AbortController; promise: Promise<HistoricalHydrationState> }>()
  private pageRequests = new Map<string, { revision: string; cursor: string; generation: number; controller: AbortController; promise: Promise<HistoricalHydrationState> }>()
  private generations = new Map<string, number>()
  private listeners = new Map<string, Set<() => void>>()

  constructor(fetchChildren: HistoricalChildrenClient) {
    this.fetchChildren = fetchChildren
  }

  get(manifest: HistoricalNodeManifest): HistoricalHydrationState {
    const key = keyOf(manifest)
    const existing = this.states.get(key)
    if (existing) return existing
    const idle: HistoricalHydrationState = { status: 'idle', manifest, children: [], nextCursor: null, hasMore: false, loadingMore: false }
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
    this.set(key, { status: 'loading', manifest, children: [], nextCursor: null, hasMore: false, loadingMore: false })
    const promise = this.fetchChildren(manifest, controller.signal)
      .then((response) => assertResponse(response, manifest))
      .then((response) => {
        if (controller.signal.aborted || this.requests.get(key)?.promise !== promise) return this.get(manifest)
        const state: HistoricalHydrationState = { status: 'ready', manifest: response.parent, children: response.children, nextCursor: response.nextCursor ?? null, hasMore: response.hasMore ?? false, loadingMore: false }
        this.set(key, state)
        return state
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || this.requests.get(key)?.promise !== promise) return this.get(manifest)
        const state: HistoricalHydrationState = { status: 'error', manifest, children: [], nextCursor: null, hasMore: false, loadingMore: false, error }
        this.set(key, state)
        return state
      })
      .finally(() => {
        if (this.requests.get(key)?.promise === promise) this.requests.delete(key)
      })
    this.requests.set(key, { revision: manifest.revision, controller, promise })
    return promise
  }

  loadMore(manifest: HistoricalNodeManifest): Promise<HistoricalHydrationState> {
    const key = keyOf(manifest)
    const current = this.get(manifest)
    if (!current.hasMore || !current.nextCursor) return Promise.resolve(current)
    const active = this.pageRequests.get(key)
    if (active?.revision === current.manifest.revision && active.cursor === current.nextCursor) return active.promise

    active?.controller.abort()
    const controller = new AbortController()
    const cursor = current.nextCursor
    const revision = current.manifest.revision
    const generation = this.generations.get(key) ?? 0
    this.set(key, { ...current, loadingMore: true, pageError: undefined })
    const promise = this.fetchChildren(current.manifest, controller.signal, cursor)
      .then((response) => assertResponse(response, current.manifest))
      .then((response) => {
        const latest = this.states.get(key)
        const request = this.pageRequests.get(key)
        if (controller.signal.aborted || request?.promise !== promise || request.generation !== (this.generations.get(key) ?? 0)
          || latest?.manifest.revision !== revision || latest.nextCursor !== cursor) return this.get(manifest)
        const seen = new Set(latest.children.map((child) => child.nodeId))
        const state: HistoricalHydrationState = {
          status: 'ready', manifest: response.parent,
          children: [...latest.children, ...response.children.filter((child) => !seen.has(child.nodeId))],
          nextCursor: response.nextCursor ?? null, hasMore: response.hasMore ?? false, loadingMore: false,
        }
        this.set(key, state)
        return state
      })
      .catch((error: unknown) => {
        const latest = this.states.get(key)
        if (controller.signal.aborted || this.pageRequests.get(key)?.promise !== promise || !latest) return this.get(manifest)
        const state = { ...latest, loadingMore: false, pageError: error }
        this.set(key, state)
        return state
      })
      .finally(() => {
        if (this.pageRequests.get(key)?.promise === promise) this.pageRequests.delete(key)
      })
    this.pageRequests.set(key, { revision, cursor, generation, controller, promise })
    return promise
  }

  collapse(manifest: HistoricalNodeManifest): void {
    const key = keyOf(manifest)
    this.requests.get(key)?.controller.abort()
    this.requests.delete(key)
    this.cancelPageRequest(key)
    const state = this.states.get(key)
    if (state?.status === 'loading') this.set(key, { ...state, status: state.children.length ? 'stale' : 'idle' })
    else if (state?.loadingMore) this.set(key, { ...state, loadingMore: false })
  }

  invalidate(manifest: HistoricalNodeManifest, newRevision: string): void {
    const key = keyOf(manifest)
    this.requests.get(key)?.controller.abort()
    this.requests.delete(key)
    this.cancelPageRequest(key)
    this.generations.set(key, (this.generations.get(key) ?? 0) + 1)
    const current = this.states.get(key)
    this.set(key, {
      status: 'stale',
      manifest: { ...(current?.manifest ?? manifest), revision: newRevision },
      children: [],
      nextCursor: null,
      hasMore: false,
      loadingMore: false,
    })
  }

  tombstone(manifest: Pick<HistoricalNodeManifest, 'sessionId' | 'nodeId'>): void {
    const key = keyOf(manifest)
    this.requests.get(key)?.controller.abort()
    this.requests.delete(key)
    this.cancelPageRequest(key)
    this.generations.set(key, (this.generations.get(key) ?? 0) + 1)
    this.states.delete(key)
    this.emit(key)
  }

  private set(key: string, state: HistoricalHydrationState): void {
    this.states.set(key, state)
    this.emit(key)
  }

  private cancelPageRequest(key: string): void {
    this.pageRequests.get(key)?.controller.abort()
    this.pageRequests.delete(key)
  }

  private emit(key: string): void {
    this.listeners.get(key)?.forEach((listener) => listener())
  }
}
