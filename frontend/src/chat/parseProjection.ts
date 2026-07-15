import type { BodyItem, CanonicalResult, ChatItem, ChatProjection, ModelChange, ScopedTurn, Turn } from './model'

export const PROJECTION_LIMITS = {
  bytes: 8_000_000,
  nodes: 10_000,
  depth: 2_048,
  stringBytes: 1_000_000,
  scalars: 100_000,
  ids: 50_000,
  arrays: 20_000,
} as const

export type ProjectionErrorCode =
  | 'invalid_type' | 'unknown_field' | 'missing_field' | 'unknown_variant'
  | 'bytes_exceeded' | 'nodes_exceeded' | 'depth_exceeded' | 'string_bytes_exceeded'
  | 'scalars_exceeded' | 'ids_exceeded' | 'arrays_exceeded'

export class ProjectionParseError extends Error {
  readonly code: ProjectionErrorCode
  readonly path: string

  constructor(code: ProjectionErrorCode, path: string, message: string) {
    super(`${path}: ${message}`)
    this.name = 'ProjectionParseError'
    this.code = code
    this.path = path
  }
}

type RecordValue = Record<string, unknown>
type NodeTask = { kind: 'node'; value: unknown; path: string; depth: number; assign: (item: BodyItem) => void }
type FinalizeTask = { kind: 'finalize'; slots: BodyItem[]; assign: (item: BodyItem) => void }
type Pending = NodeTask | FinalizeTask
type Budget = { ids: number; nodes: number }

function fail(code: ProjectionErrorCode, path: string, message: string): never {
  throw new ProjectionParseError(code, path, message)
}

function record(value: unknown, path: string): RecordValue {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail('invalid_type', path, 'expected object')
  return value as RecordValue
}

function exact(value: RecordValue, keys: readonly string[], path: string): void {
  const allowed = new Set(keys)
  for (const key of Object.keys(value)) if (!allowed.has(key)) fail('unknown_field', `${path}.${key}`, 'unknown field')
  for (const key of keys) if (!(key in value)) fail('missing_field', `${path}.${key}`, 'missing field')
}

function string(value: unknown, path: string): string {
  if (typeof value !== 'string') fail('invalid_type', path, 'expected string')
  if (utf8(value) > PROJECTION_LIMITS.stringBytes) fail('string_bytes_exceeded', path, 'string too long')
  return value
}

function id(value: unknown, path: string, budget: Budget): string {
  if (++budget.ids > PROJECTION_LIMITS.ids) fail('ids_exceeded', path, 'too many identifiers')
  return string(value, path)
}

function ids(value: unknown, path: string, budget: Budget): string[] {
  if (!Array.isArray(value)) fail('invalid_type', path, 'expected array')
  return value.map((item, index) => id(item, `${path}[${index}]`, budget))
}

function result(value: unknown, path: string, budget: Budget): CanonicalResult | null {
  if (value === null) return null
  const data = record(value, path)
  exact(data, ['type', 'part_ids', 'text'], path)
  const type = string(data.type, `${path}.type`)
  if (type !== 'ProviderResult' && type !== 'DerivedResult') fail('unknown_variant', `${path}.type`, type)
  return { type, partIds: ids(data.part_ids, `${path}.part_ids`, budget), text: string(data.text, `${path}.text`) }
}

export type ProjectionAdmission = { bytes: number; scalars: number; arrays: number }

export function measureProjectionInput(value: unknown): ProjectionAdmission {
  type AdmissionTask = { kind: 'value'; value: unknown } | { kind: 'exit'; value: object }
  const pending: AdmissionTask[] = [{ kind: 'value', value }]
  const active = new WeakSet<object>()
  let bytes = 0
  let scalars = 0
  let arrays = 0
  while (pending.length > 0) {
    const task = pending.pop()!
    if (task.kind === 'exit') { active.delete(task.value); continue }
    const item = task.value
    if (item === null || typeof item === 'boolean' || typeof item === 'number' || typeof item === 'string') {
      if (++scalars > PROJECTION_LIMITS.scalars) fail('scalars_exceeded', '$', 'too many scalar values')
      bytes += scalarBytes(item)
    } else if (Array.isArray(item)) {
      if (active.has(item)) fail('invalid_type', '$', 'cyclic input')
      active.add(item)
      pending.push({ kind: 'exit', value: item })
      if (++arrays > PROJECTION_LIMITS.arrays) fail('arrays_exceeded', '$', 'too many arrays')
      bytes += 2 + Math.max(0, item.length - 1)
      for (const child of item) pending.push({ kind: 'value', value: child })
    } else if (item && typeof item === 'object') {
      if (active.has(item)) fail('invalid_type', '$', 'cyclic input')
      active.add(item)
      pending.push({ kind: 'exit', value: item })
      const entries = Object.entries(item)
      bytes += 2 + Math.max(0, entries.length - 1)
      for (const [key, child] of entries) {
        bytes += quotedBytes(key) + 1
        pending.push({ kind: 'value', value: child })
      }
    } else fail('invalid_type', '$', 'non-JSON input')
    if (bytes > PROJECTION_LIMITS.bytes) fail('bytes_exceeded', '$', 'projection payload too large')
  }
  return { bytes, scalars, arrays }
}

export function parseProjection(value: unknown): ChatProjection {
  measureProjectionInput(value)
  if (!Array.isArray(value)) fail('invalid_type', '$', 'expected array')
  const output: ChatItem[] = []
  const pending: Pending[] = []
  const budget: Budget = { ids: 0, nodes: 0 }

  for (let index = value.length - 1; index >= 0; index -= 1) {
    countNode(budget, `$[${index}]`)
    const data = record(value[index], `$[${index}]`)
    const type = string(data.type, `$[${index}].type`)
    if (type === 'ModelChange') {
      exact(data, ['type', 'id', 'before_turn'], `$[${index}]`)
      output.unshift(modelChange(data, `$[${index}]`, budget))
      continue
    }
    if (type !== 'Turn') fail('unknown_variant', `$[${index}].type`, type)
    exact(data, ['type', 'id', 'prompt', 'body', 'result'], `$[${index}]`)
    if (!Array.isArray(data.body)) fail('invalid_type', `$[${index}].body`, 'expected array')
    const turn: Turn = { type, id: id(data.id, `$[${index}].id`, budget), prompt: id(data.prompt, `$[${index}].prompt`, budget), body: [], result: result(data.result, `$[${index}].result`, budget) }
    output.unshift(turn)
    pushChildren(pending, data.body, `$[${index}].body`, 1, (item) => turn.body.push(item))
  }

  while (pending.length > 0) {
    const task = pending.pop()!
    if (task.kind === 'finalize') {
      for (const item of task.slots) task.assign(item)
      continue
    }
    countNode(budget, task.path)
    if (task.depth > PROJECTION_LIMITS.depth) fail('depth_exceeded', task.path, 'projection nesting too deep')
    parseBodyNode(task, pending, budget)
  }
  return output
}

function parseBodyNode(task: NodeTask, pending: Pending[], budget: Budget): void {
  const data = record(task.value, task.path)
  const type = string(data.type, `${task.path}.type`)
  if (type === 'Explanation') {
    exact(data, ['type', 'text', 'text_event_ids', 'item_ids'], task.path)
    task.assign({ type, text: string(data.text, `${task.path}.text`), textEventIds: ids(data.text_event_ids, `${task.path}.text_event_ids`, budget), itemIds: ids(data.item_ids, `${task.path}.item_ids`, budget) })
    return
  }
  if (type === 'SteeringMessage') {
    exact(data, ['type', 'id', 'text'], task.path)
    task.assign({ type, id: id(data.id, `${task.path}.id`, budget), text: string(data.text, `${task.path}.text`) })
    return
  }
  if (type !== 'NativeSubagentTurn' && type !== 'WorkerTurn') fail('unknown_variant', `${task.path}.type`, type)
  exact(data, ['type', 'id', 'prompt', 'body', 'result', 'children'], task.path)
  if (!Array.isArray(data.body)) fail('invalid_type', `${task.path}.body`, 'expected array')
  const scoped: ScopedTurn = { type, id: id(data.id, `${task.path}.id`, budget), prompt: string(data.prompt, `${task.path}.prompt`), body: [], result: result(data.result, `${task.path}.result`, budget), children: ids(data.children, `${task.path}.children`, budget) }
  task.assign(scoped)
  pushChildren(pending, data.body, `${task.path}.body`, task.depth + 1, (item) => scoped.body.push(item))
}

function modelChange(data: RecordValue, path: string, budget: Budget): ModelChange {
  return { type: 'ModelChange', id: id(data.id, `${path}.id`, budget), beforeTurn: id(data.before_turn, `${path}.before_turn`, budget) }
}

function pushChildren(pending: Pending[], body: unknown[], path: string, depth: number, assign: (item: BodyItem) => void): void {
  if (body.length === 0) return
  const slots = new Array<BodyItem>(body.length)
  pending.push({ kind: 'finalize', slots, assign })
  for (let index = body.length - 1; index >= 0; index -= 1) {
    pending.push({ kind: 'node', value: body[index], path: `${path}[${index}]`, depth, assign: (item) => { slots[index] = item } })
  }
}

function countNode(budget: Budget, path: string): void {
  if (++budget.nodes > PROJECTION_LIMITS.nodes) fail('nodes_exceeded', path, 'too many canonical nodes')
}

function scalarBytes(value: string | number | boolean | null): number {
  if (typeof value === 'string') return quotedBytes(value)
  if (value === null) return 4
  if (typeof value === 'boolean') return value ? 4 : 5
  if (!Number.isFinite(value)) fail('invalid_type', '$', 'non-finite number')
  return utf8(JSON.stringify(value))
}

function quotedBytes(value: string): number {
  return utf8(JSON.stringify(value))
}

function utf8(value: string): number {
  return new TextEncoder().encode(value).length
}
