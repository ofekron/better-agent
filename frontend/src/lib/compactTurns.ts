import type { AskResult, ChatMessage, Session, UserInputRequest, WSEvent } from 'src/types'

export type CompactManifest = {
  id: string
  type: string
  revision: string
  direct_child_count: number
  display_summary: string
}

type CompactActionableCard = {
  type: 'propose_sessions'
  status: 'pending' | 'resolved'
  ask_result: AskResult
  chosen_session_id: string | null
}

type CompactTurn = {
  id: string
  start_seq: number | null
  end_seq: number | null
  prompt: { id: string | null; content: string }
  assistant: {
    id: string | null
    final_visible_text: string
    running: boolean
    hydration_root: CompactManifest | null
    visible_text_groups: Array<CompactManifest & { text: string }>
    actionable_cards: CompactActionableCard[]
    boundary_events: WSEvent[]
  }
}

export type CompactTurnPage = {
  session_id: string
  session: Session
  incarnation: string
  render_revision: number
  events_watermark: number
  turns: CompactTurn[]
  page_cursor: { before_seq: number | null; has_older: boolean; revision: string }
  pending_user_inputs: UserInputRequest[]
  pending_user_inputs_revision?: number
}

export type CompactRenderDelta =
  | { op: 'replace_turn'; sid: string; turn_id: string; turn: CompactTurn }
  | { op: 'delete_turn'; sid: string; turn_id: string }
  | { op: 'truncate_after_seq'; sid: string; keep_count: number; after_seq: number | null }
  | { op: 'session_delete'; sid: string }
  | { op: 'session_view'; sid: string; [key: string]: unknown }

export type CompactTurnsState = CompactTurnPage & { status: 'ready' | 'deleted' }

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

function isCompactTurn(value: unknown): value is CompactTurn {
  if (!isRecord(value) || typeof value.id !== 'string') return false
  if (!isRecord(value.prompt) || !(typeof value.prompt.id === 'string' || value.prompt.id === null) || typeof value.prompt.content !== 'string') return false
  const assistant = value.assistant
  if (!isRecord(assistant)) return false
  return (typeof assistant.id === 'string' || assistant.id === null)
    && typeof assistant.final_visible_text === 'string'
    && typeof assistant.running === 'boolean'
    && Array.isArray(assistant.visible_text_groups)
    && Array.isArray(assistant.actionable_cards)
    && Array.isArray(assistant.boundary_events)
}

export function parseCompactTurnPage(value: unknown): CompactTurnPage {
  if (!isRecord(value)
    || typeof value.session_id !== 'string'
    || !isRecord(value.session)
    || typeof value.incarnation !== 'string'
    || typeof value.render_revision !== 'number'
    || typeof value.events_watermark !== 'number'
    || !Array.isArray(value.turns)
    || !value.turns.every(isCompactTurn)
    || !isRecord(value.page_cursor)
    || typeof value.page_cursor.has_older !== 'boolean'
    || !Array.isArray(value.pending_user_inputs)) {
    throw new Error('Invalid compact turn page')
  }
  return value as unknown as CompactTurnPage
}

function parseCompactRenderDelta(value: unknown): CompactRenderDelta {
  if (!isRecord(value) || typeof value.op !== 'string' || typeof value.sid !== 'string') throw new Error('Invalid compact render delta')
  if (value.op === 'session_view' || value.op === 'session_delete') return value as CompactRenderDelta
  if (value.op === 'replace_turn' && typeof value.turn_id === 'string' && isCompactTurn(value.turn)) return value as unknown as CompactRenderDelta
  if (value.op === 'delete_turn' && typeof value.turn_id === 'string') return value as unknown as CompactRenderDelta
  if (value.op === 'truncate_after_seq' && typeof value.keep_count === 'number' && (typeof value.after_seq === 'number' || value.after_seq === null)) return value as unknown as CompactRenderDelta
  throw new Error('Invalid compact render delta')
}

export function compactTurnsToMessages(turns: CompactTurn[]): ChatMessage[] {
  return turns.flatMap((turn) => {
    const messages: ChatMessage[] = []
    if (turn.prompt.id || turn.prompt.content) {
      messages.push({
        id: turn.prompt.id ?? `${turn.id}:prompt`,
        role: 'user',
        content: turn.prompt.content,
        seq: turn.start_seq ?? undefined,
        events: [],
        isStreaming: false,
      })
    }
    const card = turn.assistant.actionable_cards.find((candidate) => candidate.type === 'propose_sessions')
    if (turn.assistant.id || turn.assistant.final_visible_text || card) {
      messages.push({
        id: turn.assistant.id ?? `${turn.id}:assistant`,
        role: 'assistant',
        content: turn.assistant.final_visible_text,
        seq: turn.end_seq ?? undefined,
        isStreaming: turn.assistant.running,
        ask_result: card?.ask_result,
        chosen_session_id: card?.chosen_session_id ?? undefined,
        historical_hydration_root: turn.assistant.hydration_root,
        events: turn.assistant.boundary_events,
        workers: [],
      })
    }
    return messages
  })
}

export function mergeCompactWithLiveMessages(compact: ChatMessage[], live: ChatMessage[]): ChatMessage[] {
  const byId = new Map(compact.map((message) => [message.id, message]))
  const compactIds = new Set(byId.keys())
  const compactMaxSeq = compact.reduce((maximum, message) => Math.max(maximum, message.seq ?? -1), -1)
  const beyondSnapshot = live.filter((message) => (message.seq ?? -1) > compactMaxSeq)
  let latestPromptIndex = -1
  for (let index = beyondSnapshot.length - 1; index >= 0; index -= 1) {
    if (beyondSnapshot[index].role !== 'user') continue
    latestPromptIndex = index
    break
  }
  const latestTurnIds = new Set(
    (latestPromptIndex >= 0 ? beyondSnapshot.slice(latestPromptIndex) : beyondSnapshot.slice(-1))
      .map((message) => message.id),
  )
  for (const message of live) {
    if (!compactIds.has(message.id) && !message.isStreaming && !latestTurnIds.has(message.id)) continue
    const existing = byId.get(message.id)
    if (!existing) {
      byId.set(message.id, message)
      continue
    }
    const liveEventIds = new Set((message.events ?? []).map((event) => event.uuid))
    const missingBoundaries = (existing.events ?? []).filter(
      (event) => event.type === 'model_switched' && !liveEventIds.has(event.uuid),
    )
    byId.set(message.id, {
      ...existing,
      ...message,
      events: [...(message.events ?? []), ...missingBoundaries],
    })
  }
  return [...byId.values()].sort((left, right) => (left.seq ?? Number.MAX_SAFE_INTEGER) - (right.seq ?? Number.MAX_SAFE_INTEGER))
}

export function mergeOlderCompactTurns(current: CompactTurnsState, page: CompactTurnPage): CompactTurnsState {
  if (page.session_id !== current.session_id || page.incarnation !== current.incarnation) throw new Error('Compact page fence mismatch')
  const seen = new Set(current.turns.map((turn) => turn.id))
  const older = page.turns.filter((turn) => !seen.has(turn.id))
  return {
    ...current,
    turns: [...older, ...current.turns],
    page_cursor: page.page_cursor,
  }
}

export function applyCompactRenderDelta(
  current: CompactTurnsState,
  envelope: { incarnation: string; render_revision: number; delta: CompactRenderDelta },
): CompactTurnsState {
  if (envelope.incarnation !== current.incarnation || envelope.render_revision !== current.render_revision + 1) {
    throw new Error('Compact render revision gap')
  }
  const revision = envelope.render_revision
  const delta = parseCompactRenderDelta(envelope.delta)
  const cursorParts = current.page_cursor.revision.split(':')
  const cursorRevision = cursorParts.length >= 3
    ? `${envelope.incarnation}:${revision}:${cursorParts.at(-1)}`
    : `${envelope.incarnation}:${revision}`
  const advanced = {
    ...current,
    render_revision: revision,
    page_cursor: { ...current.page_cursor, revision: cursorRevision },
  }
  if (delta.sid !== current.session_id) return advanced
  if (delta.op === 'session_delete') return { ...advanced, status: 'deleted', turns: [] }
  if (delta.op === 'session_view') return advanced
  if (delta.op === 'delete_turn') {
    return { ...advanced, turns: current.turns.filter((turn) => turn.id !== delta.turn_id) }
  }
  if (delta.op === 'truncate_after_seq') {
    return {
      ...advanced,
      turns: current.turns.filter((turn) => turn.end_seq === null || delta.after_seq === null || turn.end_seq <= delta.after_seq),
    }
  }
  const index = current.turns.findIndex((turn) => turn.id === delta.turn_id)
  const turns = index < 0
    ? [...current.turns, delta.turn]
    : current.turns.map((turn, turnIndex) => turnIndex === index ? delta.turn : turn)
  return { ...advanced, turns }
}
