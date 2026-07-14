import { describe, expect, it } from 'vitest'
import { applyCompactRenderDelta, compactTurnsToMessages, mergeCompactWithLiveMessages, mergeOlderCompactTurns, type CompactTurnPage, type CompactTurnsState } from 'src/lib/compactTurns'

type CompactTurn = CompactTurnPage['turns'][number]

const turn = (id: string, seq: number): CompactTurn => ({
  id, start_seq: seq, end_seq: seq, prompt: { id: `p-${id}`, content: id },
  assistant: { id: `a-${id}`, final_visible_text: id, running: false, hydration_root: null, visible_text_groups: [], actionable_cards: [], boundary_events: [] },
})
const state = (): CompactTurnsState => ({
  status: 'ready', session_id: 's', incarnation: 'i', render_revision: 2,
  events_watermark: 8,
  turns: [turn('b', 2)], page_cursor: { before_seq: 2, has_older: true, revision: 'i:2' }, pending_user_inputs: [],
})

describe('compact turn projection', () => {
  it('prepends older pages in backend order without duplicates', () => {
    const pending = [{ id: 'current-request' }] as CompactTurnsState['pending_user_inputs']
    const current = { ...state(), pending_user_inputs: pending }
    const merged = mergeOlderCompactTurns(state(), {
      ...state(), pending_user_inputs: [], turns: [turn('a', 1), turn('b', 2)], page_cursor: { before_seq: 1, has_older: false, revision: 'i:2' },
    })
    expect(merged.turns.map(({ id }) => id)).toEqual(['a', 'b'])
    expect(mergeOlderCompactTurns(current, { ...state(), pending_user_inputs: [] }).pending_user_inputs).toBe(pending)
  })

  it('orders replacements/appends and applies tombstones', () => {
    const appended = applyCompactRenderDelta(state(), { incarnation: 'i', render_revision: 3, delta: { op: 'replace_turn', sid: 's', turn_id: 'c', turn: turn('c', 3) } })
    expect(appended.turns.map(({ id }) => id)).toEqual(['b', 'c'])
    const deleted = applyCompactRenderDelta(appended, { incarnation: 'i', render_revision: 4, delta: { op: 'delete_turn', sid: 's', turn_id: 'b' } })
    expect(deleted.turns.map(({ id }) => id)).toEqual(['c'])
  })

  it('replaces a user-only turn when its assistant arrives without duplicating it', () => {
    const userOnly = turn('stable', 1)
    userOnly.assistant = { id: null, final_visible_text: '', running: false, hydration_root: null, visible_text_groups: [], actionable_cards: [], boundary_events: [] }
    const current = { ...state(), turns: [userOnly] }
    const completed = turn('stable', 1)
    completed.assistant.id = 'assistant-later'
    const updated = applyCompactRenderDelta(current, {
      incarnation: 'i', render_revision: 3,
      delta: { op: 'replace_turn', sid: 's', turn_id: 'stable', turn: completed },
    })
    expect(updated.turns).toHaveLength(1)
    expect(updated.turns[0].assistant.id).toBe('assistant-later')
  })

  it('rejects stale, skipped, and foreign-incarnation revisions', () => {
    const delta = { op: 'session_view' as const, sid: 's' }
    expect(() => applyCompactRenderDelta(state(), { incarnation: 'i', render_revision: 2, delta })).toThrow()
    expect(() => applyCompactRenderDelta(state(), { incarnation: 'i', render_revision: 4, delta })).toThrow()
    expect(() => applyCompactRenderDelta(state(), { incarnation: 'other', render_revision: 3, delta })).toThrow()
  })

  it('preserves turn order and the exact existing propose-sessions picker contract', () => {
    const projected = turn('a', 1)
    projected.assistant.actionable_cards = [{
      type: 'propose_sessions',
      status: 'pending',
      ask_result: {
        results: [{ id: 'target', name: 'Target', cwd: '/tmp', first_user_prompt: 'prompt' }],
        reasoning: 'best match',
        proposed_project_path: '/project',
      },
      chosen_session_id: null,
    }]
    const messages = compactTurnsToMessages([projected, turn('b', 2)])
    expect(messages.map(({ id }) => id)).toEqual(['p-a', 'a-a', 'p-b', 'a-b'])
    expect(messages[1].ask_result).toEqual(projected.assistant.actionable_cards[0].ask_result)
  })

  it('replaces compact latest content with the authoritative live message without duplication', () => {
    const compact = compactTurnsToMessages([turn('a', 1)])
    const live = { ...compact[1], content: 'streamed', isStreaming: true }
    const merged = mergeCompactWithLiveMessages(compact, [live])
    expect(merged).toHaveLength(2)
    expect(merged[1]).toMatchObject({ id: 'a-a', content: 'streamed', isStreaming: true })
  })

  it('keeps compact model boundaries when a thin live assistant has no events', () => {
    const projected = turn('a', 1)
    projected.assistant.boundary_events = [{
      uuid: 'model-boundary', type: 'model_switched', timestamp: '2026-01-01T00:00:00Z',
      data: { provider: 'codex', model: 'gpt-5' },
    }]
    const compact = compactTurnsToMessages([projected])
    const merged = mergeCompactWithLiveMessages(compact, [{ ...compact[1], events: [] }])
    expect(merged[1].events).toEqual(projected.assistant.boundary_events)
  })

  it('keeps the newest WS turn after completion until its compact delta arrives', () => {
    const prompt = { id: 'p-live', role: 'user' as const, content: 'prompt', events: [], isStreaming: false, seq: 1 }
    const answer = { id: 'a-live', role: 'assistant' as const, content: 'final', events: [], isStreaming: false, seq: 2 }
    expect(mergeCompactWithLiveMessages([], [prompt, answer]).map(({ id }) => id)).toEqual(['p-live', 'a-live'])
  })

  it('never widens five compact turns with fifty completed historical messages', () => {
    const compact = compactTurnsToMessages([turn('46', 46), turn('47', 47), turn('48', 48), turn('49', 49), turn('50', 50)])
    const historical = Array.from({ length: 50 }, (_, index) => ({
      id: `historical-${index + 1}`, role: 'assistant' as const, content: String(index), events: [], isStreaming: false,
    }))
    const live = { id: 'live-51', role: 'assistant' as const, content: 'live', events: [], isStreaming: true, seq: 51 }
    const merged = mergeCompactWithLiveMessages(compact, [...historical, live])
    expect(merged.filter((message) => message.role === 'user')).toHaveLength(5)
    expect(merged.some((message) => message.id === 'historical-1')).toBe(false)
    expect(merged.some((message) => message.id === 'live-51')).toBe(true)
  })

  it('rejects a malformed replace-turn before state mutation', () => {
    expect(() => applyCompactRenderDelta(state(), {
      incarnation: 'i', render_revision: 3,
      delta: { op: 'replace_turn', sid: 's', turn_id: 'bad', turn: {} } as never,
    })).toThrow('Invalid compact render delta')
  })
})
