import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { decorateModelRuns } from 'src/chat/decorateModelRuns'
import type { RunMeta, ScopedTurn, Turn, VisibleEvent } from 'src/chat/model'
import { measureProjectionInput, parseProjection, ProjectionParseError, PROJECTION_LIMITS } from 'src/chat/parseProjection'
import { explanationPlan, oneLevelPlan, renderTurnPlan } from 'src/chat/renderPlan'

type Fixture = {
  events: Array<{ journal_seq: number; event_id: string; context_id: string; provider: { id: string; model: string; effort: string } }>
  expected: {
    chat_tree_completed: unknown
    formal_edge_cases: { visible_plans: Record<string, string[]> }
    model_marker_targets: Record<'live-at-seq-22' | 'completed-at-seq-33', MarkerTarget[]> & {
      visible_render_plans: Record<string, { scope?: string; visible_event_ids: string[]; panel_last_id?: string; marker_target_id: string }>
    }
  }
}

type MarkerTarget = { scope: string; provider: string; model: string; effort: string; target_event_id: string }

const fixture = JSON.parse(readFileSync(resolve(import.meta.dirname, '../../test-contracts/chat-panel/v1/canonical-session.json'), 'utf8')) as Fixture
const projection = parseProjection(fixture.expected.chat_tree_completed)
const turn = (id: string) => projection.find((item): item is Turn => item.type === 'Turn' && item.id === id)!

function tokens(id: string, mode: 'collapsed' | 'extended' | 'live'): string[] {
  return renderTurnPlan(turn(id), mode).map((token) => {
    if (token.kind === 'prompt') return `prompt:${token.id}`
    if (token.kind === 'ellipsis') return `ellipsis:${token.id}`
    if (token.kind === 'result') return `result:${token.ids.join('+')}`
    if (token.kind === 'explanation') return `${token.expanded ? 'text' : 'summary'}:${token.text}`
    return `${token.kind}:${token.id}`
  })
}

describe('canonical chat projection parser', () => {
  it('parses the accepted backend-emitted formal tree without rederiving ownership', () => {
    expect(projection.map((item) => item.type === 'Turn' ? item.id : item.id)).toEqual(['mc1', 'turn-1', 'turn-2', 'turn-3', 'turn-4', 'turn-5', 'turn-6', 'turn-7'])
    expect(turn('turn-2').body.map((item) => item.type)).toEqual(['Explanation', 'Explanation', 'SteeringMessage', 'NativeSubagentTurn', 'WorkerTurn'])
    expect(turn('turn-4').body[1]).toMatchObject({ type: 'WorkerTurn', id: 'e-live-worker', body: [{ type: 'NativeSubagentTurn', id: 'e-live-native' }] })
  })

  it('fails closed on unknown variants, fields, types, and oversized inputs', () => {
    expectCode([{ type: 'Alien' }], 'unknown_variant')
    expectCode([{ type: 'Turn', id: 't', prompt: 'p', body: [], result: null, surprise: true }], 'unknown_field')
    expectCode([{ type: 'Turn', id: 1, prompt: 'p', body: [], result: null }], 'invalid_type')
  })

  it('admits exactly 10,000 canonical nodes and rejects the next node', () => {
    const nodes = Array.from({ length: PROJECTION_LIMITS.nodes }, (_, index) => ({ type: 'ModelChange', id: `m${index}`, before_turn: `t${index}` }))
    expect(parseProjection(nodes)).toHaveLength(PROJECTION_LIMITS.nodes)
    expectCode([...nodes, { type: 'ModelChange', id: 'overflow', before_turn: 'overflow' }], 'nodes_exceeded')
  })

  it('measures exact compact JSON UTF-8 bytes and enforces aggregate budgets independently', () => {
    const measured = ['é', { value: '🙂' }]
    expect(measureProjectionInput(measured).bytes).toBe(new TextEncoder().encode(JSON.stringify(measured)).length)
    const exactBytes = ['x'.repeat(PROJECTION_LIMITS.bytes - 4)]
    expect(new TextEncoder().encode(JSON.stringify(exactBytes)).length).toBe(PROJECTION_LIMITS.bytes)
    expect(measureProjectionInput(exactBytes).bytes).toBe(PROJECTION_LIMITS.bytes)
    const overBytes = ['x'.repeat(PROJECTION_LIMITS.bytes - 3)]
    expect(new TextEncoder().encode(JSON.stringify(overBytes)).length).toBe(PROJECTION_LIMITS.bytes + 1)
    expectAdmissionCode(overBytes, 'bytes_exceeded')
    expectCode(Array.from({ length: PROJECTION_LIMITS.scalars + 1 }, () => null), 'scalars_exceeded')
    expectCode(Array.from({ length: PROJECTION_LIMITS.arrays }, () => []), 'arrays_exceeded')
    expectCode(Array.from({ length: 9 }, () => 'x'.repeat(999_999)), 'bytes_exceeded')
    const tooManyIds = [{ type: 'Turn', id: 't', prompt: 'u', body: [{ type: 'Explanation', text: '', text_event_ids: [], item_ids: Array.from({ length: PROJECTION_LIMITS.ids }, (_, index) => `id-${index}`) }], result: null }]
    expectCode(tooManyIds, 'ids_exceeded')
  })

  it('rejects a maliciously deep tree without overflowing the stack', () => {
    let body: unknown[] = []
    for (let depth = 0; depth < 2_000; depth += 1) body = [{ type: 'WorkerTurn', id: `w${depth}`, prompt: 'p', body, result: null, children: [] }]
    expect(parseProjection([{ type: 'Turn', id: 'accepted', prompt: 'p', body, result: null }])).toHaveLength(1)
    for (let depth = 2_000; depth < 2_100; depth += 1) body = [{ type: 'WorkerTurn', id: `w${depth}`, prompt: 'p', body, result: null, children: [] }]
    expectCode([{ type: 'Turn', id: 't', prompt: 'p', body, result: null }], 'depth_exceeded')
  })
})

describe('canonical render plans', () => {
  it('matches completed collapsed and extended fixture plans', () => {
    expect(tokens('turn-1', 'collapsed')).toEqual(['prompt:u1', 'ellipsis:turn-1', 'result:e-final-card+e-final-text'])
    expect(tokens('turn-3', 'collapsed')).toEqual(['prompt:u3'])
    expect(tokens('turn-2', 'extended')).toEqual(['prompt:u2', 'summary:Resolved after ownership arrived.', 'summary:Final replaced answer', 'steering:e-steer', 'native:e-ns1', 'worker:e-worker1'])
  })

  it('renders off-path live explanations collapsed and the final-path explanation fully expanded', () => {
    const synthetic: Turn = {
      type: 'Turn', id: 'live', prompt: 'u', result: null,
      body: [
        { type: 'Explanation', text: 'Earlier', textEventIds: ['text-earlier'], itemIds: ['tool-earlier'] },
        { type: 'Explanation', text: 'Current', textEventIds: ['text-current'], itemIds: ['tool-current'] },
      ],
    }
    expect(renderTurnPlan(synthetic, 'live')).toEqual([
      { kind: 'prompt', id: 'u' },
      { kind: 'explanation', text: 'Earlier', textEventIds: ['text-earlier'], itemIds: ['tool-earlier'], itemCount: 1, expanded: false },
      { kind: 'explanation', text: 'Current', textEventIds: ['text-current'], itemIds: ['tool-current'], itemCount: 1, expanded: true },
      { kind: 'compact', id: 'tool-current' },
    ])
  })

  it('expands an explanation independently with its compact body items', () => {
    const explanation = turn('turn-1').body[0]
    expect(explanation.type).toBe('Explanation')
    if (explanation.type !== 'Explanation') return
    expect(explanationPlan(explanation, false)).toEqual([{ kind: 'explanation', text: 'I will inspect the inputs.', textEventIds: ['e-text-1a'], itemIds: ['e-tool-1'], itemCount: 1, expanded: false }])
    expect(explanationPlan(explanation, true)).toEqual([
      { kind: 'explanation', text: 'I will inspect the inputs.', textEventIds: ['e-text-1a'], itemIds: ['e-tool-1'], itemCount: 1, expanded: true },
      { kind: 'compact', id: 'e-tool-1' },
    ])
  })

  it('keeps one-level scoped-turn expansion independent', () => {
    const worker = turn('turn-4').body[1] as ScopedTurn
    expect(oneLevelPlan(worker)).toEqual([{ kind: 'internal', id: 'e-live-worker' }, { kind: 'compact', id: 'e-live-native' }])
  })

  it('does not show an ellipsis for empty explanations', () => {
    const empty: Turn = { type: 'Turn', id: 'empty', prompt: 'u', body: [{ type: 'Explanation', text: '', textEventIds: [], itemIds: [] }], result: null }
    expect(renderTurnPlan(empty, 'collapsed')).toEqual([{ kind: 'prompt', id: 'u' }])
  })

  it('renders a 2000-level accepted live chain without recursion overflow', () => {
    let body: ScopedTurn[] = []
    for (let depth = 0; depth < 2_000; depth += 1) body = [{ type: 'WorkerTurn', id: `w${depth}`, prompt: 'p', body, result: null, children: body.map(({ id }) => id) }]
    const deep: Turn = { type: 'Turn', id: 'deep', prompt: 'u', body, result: null }
    expect(renderTurnPlan(deep, 'live')).toHaveLength(2_001)
  })
})

describe('visibility-dependent model markers', () => {
  it.each([
    ['live-at-seq-22', 22],
    ['completed-at-seq-33', 33],
  ] as const)('matches the complete ordered %s marker stream across every scope and run', (snapshot, watermark) => {
    const visible = canonicalEventsThrough(watermark)
    const actual = decorateModelRuns(visible).map(({ id, scope, run }) => ({
      scope,
      provider: run.provider,
      model: run.model,
      effort: run.effort,
      target_event_id: id,
    }))
    expect(actual).toEqual(fixture.expected.model_marker_targets[snapshot])
  })

  it('matches exact fixture targets for every canonical visible render plan', () => {
    for (const plan of Object.values(fixture.expected.model_marker_targets.visible_render_plans)) {
      const wanted = new Set(plan.visible_event_ids)
      const visible = canonicalEventsThrough(Number.POSITIVE_INFINITY)
        .filter(({ id }) => wanted.has(id))
        .map((event): VisibleEvent => ({ ...event, scope: plan.scope ?? event.scope }))
      expect(visible.map(({ id }) => id)).toEqual(plan.visible_event_ids)
      expect(decorateModelRuns(visible).map(({ id }) => id)).toEqual([plan.marker_target_id])
      if (plan.panel_last_id) expect(visible.at(-1)?.id).toBe(plan.panel_last_id)
    }
  })

  it('moves markers when visibility removes the former run tail', () => {
    const p1: RunMeta = { provider: 'p1', model: 'm1', effort: 'high' }
    const p2: RunMeta = { provider: 'p2', model: 'm2', effort: 'low' }
    const visible: VisibleEvent[] = [{ id: 'a', scope: 'root', run: p1 }, { id: 'b', scope: 'root', run: p1 }, { id: 'c', scope: 'root', run: p2 }]
    expect(decorateModelRuns(visible).map(({ id }) => id)).toEqual(['b', 'c'])
    expect(decorateModelRuns([visible[0], visible[2]]).map(({ id }) => id)).toEqual(['a', 'c'])
  })
})

function canonicalEventsThrough(watermark: number): VisibleEvent[] {
  return [...new Map(fixture.events.filter(({ journal_seq }) => journal_seq <= watermark).map((event) => [event.event_id, event])).values()]
    .sort((left, right) => left.journal_seq - right.journal_seq)
    .map(({ event_id, context_id, provider }) => ({ id: event_id, scope: context_id, run: { provider: provider.id, model: provider.model, effort: provider.effort } }))
}

function expectCode(value: unknown, code: ProjectionParseError['code']): void {
  try { parseProjection(value) } catch (error) {
    expect(error).toBeInstanceOf(ProjectionParseError)
    expect((error as ProjectionParseError).code).toBe(code)
    return
  }
  throw new Error(`Expected ${code}`)
}

function expectAdmissionCode(value: unknown, code: ProjectionParseError['code']): void {
  try { measureProjectionInput(value) } catch (error) {
    expect(error).toBeInstanceOf(ProjectionParseError)
    expect((error as ProjectionParseError).code).toBe(code)
    return
  }
  throw new Error(`Expected admission ${code}`)
}
