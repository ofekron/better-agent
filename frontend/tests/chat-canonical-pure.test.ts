import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { decorateModelRuns } from 'src/chat/decorateModelRuns'
import type { RunMeta, ScopedTurn, Turn, VisibleEvent } from 'src/chat/model'
import { parseProjection, ProjectionParseError } from 'src/chat/parseProjection'
import { explanationPlan, oneLevelPlan, renderTurnPlan } from 'src/chat/renderPlan'

type Fixture = {
  events: Array<{ event_id: string; context_id: string; provider: { id: string; model: string; effort: string } }>
  expected: {
    chat_tree_completed: unknown
    formal_edge_cases: { visible_plans: Record<string, string[]> }
    model_marker_targets: Record<string, unknown>
  }
}

const fixture = JSON.parse(readFileSync(resolve(import.meta.dirname, '../../test-contracts/chat-panel/v1/canonical-session.json'), 'utf8')) as Fixture
const projection = parseProjection(fixture.expected.chat_tree_completed)
const turn = (id: string) => projection.find((item): item is Turn => item.type === 'Turn' && item.id === id)!

function tokens(id: string, mode: 'collapsed' | 'extended' | 'live'): string[] {
  return renderTurnPlan(turn(id), mode).map((token) => {
    if (token.kind === 'prompt') return `prompt:${token.id}`
    if (token.kind === 'ellipsis') return `ellipsis:${token.id}`
    if (token.kind === 'result') return `result:${token.ids.join('+')}`
    if (token.kind === 'explanation') return 'explanation'
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
    expectCode(Array.from({ length: 10_001 }, () => ({ type: 'ModelChange', id: 'm', before_turn: 't' })), 'limit_exceeded')
  })

  it('rejects a maliciously deep tree without overflowing the stack', () => {
    let body: unknown[] = []
    for (let depth = 0; depth < 2_000; depth += 1) body = [{ type: 'WorkerTurn', id: `w${depth}`, prompt: 'p', body, result: null, children: [] }]
    expect(parseProjection([{ type: 'Turn', id: 'accepted', prompt: 'p', body, result: null }])).toHaveLength(1)
    for (let depth = 2_000; depth < 2_100; depth += 1) body = [{ type: 'WorkerTurn', id: `w${depth}`, prompt: 'p', body, result: null, children: [] }]
    expectCode([{ type: 'Turn', id: 't', prompt: 'p', body, result: null }], 'limit_exceeded')
  })
})

describe('canonical render plans', () => {
  it('matches completed collapsed and extended fixture plans', () => {
    expect(tokens('turn-1', 'collapsed')).toEqual(['prompt:u1', 'ellipsis:turn-1', 'result:e-final-card+e-final-text'])
    expect(tokens('turn-3', 'collapsed')).toEqual(['prompt:u3'])
    expect(tokens('turn-2', 'extended')).toEqual(['prompt:u2', 'explanation', 'explanation', 'steering:e-steer', 'native:e-ns1', 'worker:e-worker1'])
  })

  it('forces only the final live path and keeps off-path work compact', () => {
    expect(tokens('turn-4', 'live')).toEqual(fixture.expected.formal_edge_cases.visible_plans['live-turn-4'])
  })

  it('keeps explanation and one-level expansion semantics independent', () => {
    const explanation = turn('turn-1').body[0]
    expect(explanation.type).toBe('Explanation')
    if (explanation.type !== 'Explanation') return
    expect(explanationPlan(explanation, false)).toEqual([{ kind: 'explanation', text: 'I will inspect the inputs.', itemCount: 1, expanded: false }])
    const worker = turn('turn-4').body[1] as ScopedTurn
    expect(oneLevelPlan(worker)).toEqual([{ kind: 'internal', id: 'e-live-worker' }, { kind: 'compact', id: 'e-live-native' }])
  })

  it('renders a 2000-level accepted live chain without recursion overflow', () => {
    let body: ScopedTurn[] = []
    for (let depth = 0; depth < 2_000; depth += 1) body = [{ type: 'WorkerTurn', id: `w${depth}`, prompt: 'p', body, result: null, children: body.map(({ id }) => id) }]
    const deep: Turn = { type: 'Turn', id: 'deep', prompt: 'u', body, result: null }
    expect(renderTurnPlan(deep, 'live')).toHaveLength(2_001)
  })
})

describe('visibility-dependent model markers', () => {
  it('places one marker on the final visible event of every contiguous run per scope', () => {
    const expected = fixture.expected.model_marker_targets['completed-at-seq-33'] as Array<{ scope: string; provider: string; model: string; effort: string; target_event_id: string }>
    const wanted = new Set(expected.map(({ target_event_id }) => target_event_id))
    const visible = fixture.events
      .filter(({ event_id }) => wanted.has(event_id))
      .map(({ event_id, context_id, provider }): VisibleEvent => ({ id: event_id, scope: context_id, run: { provider: provider.id, model: provider.model, effort: provider.effort } }))
    expect(decorateModelRuns(visible).map(({ id }) => id)).toEqual(visible.map(({ id }) => id))
  })

  it('moves markers when visibility removes the former run tail', () => {
    const p1: RunMeta = { provider: 'p1', model: 'm1', effort: 'high' }
    const p2: RunMeta = { provider: 'p2', model: 'm2', effort: 'low' }
    const visible: VisibleEvent[] = [{ id: 'a', scope: 'root', run: p1 }, { id: 'b', scope: 'root', run: p1 }, { id: 'c', scope: 'root', run: p2 }]
    expect(decorateModelRuns(visible).map(({ id }) => id)).toEqual(['b', 'c'])
    expect(decorateModelRuns([visible[0], visible[2]]).map(({ id }) => id)).toEqual(['a', 'c'])
  })
})

function expectCode(value: unknown, code: ProjectionParseError['code']): void {
  try { parseProjection(value) } catch (error) {
    expect(error).toBeInstanceOf(ProjectionParseError)
    expect((error as ProjectionParseError).code).toBe(code)
    return
  }
  throw new Error(`Expected ${code}`)
}
