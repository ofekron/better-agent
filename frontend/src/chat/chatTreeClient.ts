import { API } from '../api'
import { parseProjection } from './parseProjection'
import type { BodyItem, ChatProjection, Turn } from './model'
import type { ChatMessage, WSEvent } from '../types'

export type ChatTreeLookupEntry =
  | {
      kind: 'message'
      role: string
      text: string
      seq?: number | null
      snapshot?: Record<string, unknown> | null
    }
  | {
      kind: 'event'
      type: string
      data: Record<string, unknown>
      message_id?: string | null
      timestamp?: string | null
      message_seq?: number | null
      run_meta?: Record<string, unknown> | null
    }

export type ChatTreePage = {
  turns: number
  before_turn: string | null
  older_cursor: string | null
  has_older: boolean
}

export type ChatTree = {
  session: Record<string, unknown>
  projection: ChatProjection
  lookup: Record<string, ChatTreeLookupEntry>
  page: ChatTreePage
}

export class ChatTreeError extends Error {
  readonly code: string
  readonly retryAfterSeconds: number | null

  constructor(code: string, message: string, retryAfterSeconds: number | null = null) {
    super(message)
    this.name = 'ChatTreeError'
    this.code = code
    this.retryAfterSeconds = retryAfterSeconds
  }
}

export async function fetchChatTree(
  sessionId: string,
  options: { turns?: number; beforeTurn?: string; signal?: AbortSignal } = {},
): Promise<ChatTree> {
  const params = new URLSearchParams()
  if (options.turns !== undefined) params.set('turns', String(options.turns))
  if (options.beforeTurn !== undefined) params.set('before_turn', options.beforeTurn)
  const query = params.toString()
  const response = await fetch(
    `${API}/api/chat-tree/${encodeURIComponent(sessionId)}${query ? `?${query}` : ''}`,
    { credentials: 'include', signal: options.signal },
  )
  if (!response.ok) {
    let code = String(response.status)
    let message = `chat tree request failed: ${response.status}`
    try {
      const detail = (await response.json())?.detail
      if (detail && typeof detail === 'object' && typeof detail.code === 'string') {
        code = detail.code
        message = typeof detail.message === 'string' ? detail.message : message
      } else if (typeof detail === 'string') {
        message = detail
      }
    } catch {
      // non-JSON error body: keep the status-based message
    }
    const retryAfter = Number(response.headers.get('retry-after'))
    throw new ChatTreeError(code, message, Number.isFinite(retryAfter) ? retryAfter : null)
  }
  const body = await response.json()
  return {
    session: body.session ?? {},
    projection: parseProjection(body.items),
    lookup: body.lookup ?? {},
    page: body.page,
  }
}

function numberOrUndefined(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function snapshotExtras(entry: ChatTreeLookupEntry | undefined): Partial<ChatMessage> {
  if (!entry || entry.kind !== 'message' || !entry.snapshot) return {}
  const { id: _id, role: _role, content: _content, seq: _seq, ...extras } = entry.snapshot as {
    id?: unknown
    role?: unknown
    content?: unknown
    seq?: unknown
  } & Record<string, unknown>
  return extras as Partial<ChatMessage>
}

function modelSwitchEvent(id: string, entry: ChatTreeLookupEntry): WSEvent | null {
  if (entry.kind !== 'event' || entry.type !== 'model_change') return null
  const data = entry.data as {
    from?: { provider?: string; model?: string; effort?: string } | null
    to?: { provider?: string; model?: string; effort?: string } | null
  }
  const target = data.to
  if (!target) return null
  const origin = data.from ?? undefined
  const changed: string[] = []
  if (origin?.provider !== target.provider) changed.push('provider_id')
  if (origin?.model !== target.model) changed.push('model')
  if (origin?.effort !== target.effort) changed.push('reasoning_effort')
  return {
    type: 'model_switched',
    data: {
      uuid: id,
      previous_provider_id: origin?.provider,
      previous_model: origin?.model,
      previous_reasoning_effort: origin?.effort,
      provider_id: target.provider,
      model: target.model,
      reasoning_effort: target.effort,
      changed,
    },
  } as WSEvent
}

function resolveAssistantMessageId(
  turn: Turn,
  lookup: Record<string, ChatTreeLookupEntry>,
): string | null {
  const candidates: string[] = []
  if (turn.result) candidates.push(...turn.result.partIds)
  for (const item of turn.body) {
    if (item.type === 'Explanation') candidates.push(...item.textEventIds, ...item.itemIds)
  }
  for (const id of candidates) {
    const entry = lookup[id]
    if (entry?.kind === 'event' && typeof entry.message_id === 'string' && entry.message_id) {
      return entry.message_id
    }
  }
  return null
}

function collectBodyEventIds(items: readonly BodyItem[]): string[] {
  const ids: string[] = []
  const stack = [...items]
  while (stack.length > 0) {
    const item = stack.shift()
    if (!item) continue
    if (item.type === 'Explanation') {
      ids.push(...item.textEventIds, ...item.itemIds)
    } else if (item.type === 'SteeringMessage') {
      ids.push(item.id)
    } else {
      ids.push(item.id)
      stack.unshift(...item.body)
      if (item.result) ids.push(...item.result.partIds)
      ids.push(...item.children)
    }
  }
  return ids
}

function eventForRender(id: string, entry: ChatTreeLookupEntry | undefined): WSEvent | null {
  if (!entry || entry.kind !== 'event') return null
  if (entry.type === 'assistant_text') {
    return { type: 'output', data: { uuid: id, output: String(entry.data.text ?? '') }, _ts: entry.timestamp ?? undefined }
  }
  if (entry.type === 'thinking') {
    return { type: 'thinking', data: { uuid: id, thought: String(entry.data.text ?? '') }, _ts: entry.timestamp ?? undefined }
  }
  if (entry.type === 'tool_interaction') {
    const status = String(entry.data.status ?? '')
    if (status === 'complete' && entry.data.output !== undefined) {
      return {
        type: 'tool_result',
        data: {
          uuid: id,
          tool_use_id: String(entry.data.tool_use_id ?? id),
          output: String(entry.data.output ?? ''),
        },
        _ts: entry.timestamp ?? undefined,
      }
    }
    return {
      type: 'tool_call',
      data: {
        uuid: id,
        tool_use_id: String(entry.data.tool_use_id ?? id),
        tool: String(entry.data.tool_name ?? entry.data.tool ?? 'tool'),
        args: entry.data.args ?? null,
      },
      _ts: entry.timestamp ?? undefined,
    }
  }
  if (entry.type === 'steering_message') {
    return { type: 'steer_prompt', data: { uuid: id, prompt: String(entry.data.text ?? '') }, _ts: entry.timestamp ?? undefined }
  }
  if (entry.type === 'model_change') {
    return modelSwitchEvent(id, entry)
  }
  return {
    type: 'diagnostic',
    data: { uuid: id, kind: entry.type, raw: entry.data },
    _ts: entry.timestamp ?? undefined,
  }
}

function turnEventsForRender(
  turn: Turn,
  lookup: Record<string, ChatTreeLookupEntry>,
): WSEvent[] {
  const ids = [
    ...collectBodyEventIds(turn.body),
    ...(turn.result?.partIds ?? []),
  ]
  const seen = new Set<string>()
  const events: WSEvent[] = []
  for (const id of ids) {
    if (seen.has(id)) continue
    seen.add(id)
    const event = eventForRender(id, lookup[id])
    if (event) events.push(event)
  }
  return events
}

/** Adapt the formal chat tree into the ChatMessage list the existing
 * Chat component renders. Structure and result resolution come from the
 * tree (the backend projector — no client-side rederivation); content
 * and message state come from the lookup sidecar. */
export function chatTreeToMessages(
  projection: ChatProjection,
  lookup: Record<string, ChatTreeLookupEntry>,
): ChatMessage[] {
  const messages: ChatMessage[] = []
  for (const item of projection) {
    if (item.type === 'ModelChange') {
      const entry = lookup[item.id]
      const boundary = entry ? modelSwitchEvent(item.id, entry) : null
      const previous = messages[messages.length - 1]
      if (boundary && previous && previous.role === 'assistant') {
        previous.events = [...(previous.events ?? []), boundary]
      }
      continue
    }
    const promptEntry = lookup[item.prompt]
    if (promptEntry?.kind === 'message') {
      messages.push({
        id: item.prompt,
        role: 'user',
        content: promptEntry.text,
        seq: numberOrUndefined(promptEntry.seq),
        events: [],
        isStreaming: false,
        ...snapshotExtras(promptEntry),
      })
    }
    const assistantId = resolveAssistantMessageId(item, lookup)
    if (!assistantId) continue
    const assistantEntry = lookup[assistantId]
    messages.push({
      id: assistantId,
      role: 'assistant',
      content: item.result?.text ?? '',
      seq: assistantEntry?.kind === 'message'
        ? numberOrUndefined(assistantEntry.seq)
        : undefined,
      events: [],
      isStreaming: false,
      ...(assistantEntry?.kind === 'message' ? snapshotExtras(assistantEntry) : {}),
    })
    messages[messages.length - 1].events = [
      ...(messages[messages.length - 1].events ?? []),
      ...turnEventsForRender(item, lookup),
    ]
  }
  return messages
}
