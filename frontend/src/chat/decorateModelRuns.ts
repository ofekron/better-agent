import type { ModelMarker, VisibleEvent } from './model'

function sameRun(left: VisibleEvent, right: VisibleEvent): boolean {
  return left.run.provider === right.run.provider && left.run.model === right.run.model && left.run.effort === right.run.effort
}

export function decorateModelRuns(events: readonly VisibleEvent[]): ModelMarker[] {
  const byScope = new Map<string, VisibleEvent[]>()
  for (const event of events) {
    const scope = byScope.get(event.scope)
    if (scope) scope.push(event)
    else byScope.set(event.scope, [event])
  }
  const markers: ModelMarker[] = []
  const marked = new Set<string>()
  for (const scoped of byScope.values()) {
    for (let index = 0; index < scoped.length; index += 1) {
      const next = scoped[index + 1]
      if (!next || !sameRun(scoped[index], next)) marked.add(scoped[index].id)
    }
  }
  for (const event of events) if (marked.has(event.id)) markers.push(event)
  return markers
}
