import { assertNever, type BodyItem, type Explanation, type RenderMode, type RenderToken, type ScopedTurn, type Turn } from './model'

type BodyMode = RenderMode | 'live-compact'
type Task = { kind: 'body'; item: BodyItem; mode: BodyMode }

export function renderTurnPlan(turn: Turn, mode: RenderMode): RenderToken[] {
  const output: RenderToken[] = [{ kind: 'prompt', id: turn.prompt }]
  if (mode === 'collapsed') {
    if (hasRenderableDescendant(turn.body)) output.push({ kind: 'ellipsis', id: turn.id })
    appendResult(output, turn)
    return output
  }
  const tasks: Task[] = []
  if (mode === 'live') pushLive(tasks, turn.body)
  else pushBody(tasks, turn.body, 'extended')
  drain(tasks, output)
  if (mode !== 'live') appendResult(output, turn)
  return output
}

export function explanationPlan(explanation: Explanation, expanded: boolean): RenderToken[] {
  return [{ kind: 'explanation', text: explanation.text, textEventIds: explanation.textEventIds, itemIds: explanation.itemIds, itemCount: explanation.itemIds.length, expanded }]
}

export function oneLevelPlan(turn: ScopedTurn): RenderToken[] {
  return [{ kind: 'internal', id: turn.id }, ...turn.children.map((id) => ({ kind: 'compact' as const, id }))]
}

function pushLive(tasks: Task[], body: BodyItem[]): void {
  for (let index = body.length - 1; index >= 0; index -= 1) {
    tasks.push({ kind: 'body', item: body[index], mode: index === body.length - 1 ? 'live' : 'live-compact' })
  }
}

function pushBody(tasks: Task[], body: BodyItem[], mode: RenderMode): void {
  for (let index = body.length - 1; index >= 0; index -= 1) tasks.push({ kind: 'body', item: body[index], mode })
}

function drain(tasks: Task[], output: RenderToken[]): void {
  while (tasks.length > 0) {
    const task = tasks.pop()!
    const { item, mode } = task
    if (item.type === 'Explanation') {
      const expanded = mode === 'live' || mode === 'extended'
      output.push(...explanationPlan(item, expanded))
      if (expanded) for (const id of item.itemIds) output.push({ kind: 'compact', id })
      continue
    }
    if (item.type === 'SteeringMessage') { output.push({ kind: 'steering', id: item.id }); continue }
    switch (item.type) {
      case 'NativeSubagentTurn':
      case 'WorkerTurn': {
        const kind = item.type === 'NativeSubagentTurn' ? 'native' : 'worker'
        output.push({ kind: mode === 'live' ? 'expanded' : kind, id: item.id })
        if (mode === 'live') {
          if (item.body.length > 0) pushLive(tasks, item.body)
          else if (item.result) for (const id of item.result.partIds) output.push({ kind: 'expanded', id })
        }
        continue
      }
      default: assertNever(item)
    }
  }
}

function hasRenderableDescendant(body: BodyItem[]): boolean {
  const pending = [...body]
  while (pending.length > 0) {
    const item = pending.pop()!
    if (item.type === 'Explanation') {
      if (item.text.length > 0 || item.itemIds.length > 0) return true
      continue
    }
    if (item.type === 'SteeringMessage') return true
    if (item.prompt.length > 0 || item.result !== null || item.children.length > 0) return true
    pending.push(...item.body)
  }
  return false
}

function appendResult(output: RenderToken[], turn: Turn): void {
  if (turn.result) output.push({ kind: 'result', ids: turn.result.partIds, text: turn.result.text })
}
