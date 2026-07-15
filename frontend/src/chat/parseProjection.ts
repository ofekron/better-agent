import type { BodyItem, CanonicalResult, ChatItem, ChatProjection, ModelChange, ScopedTurn, Turn } from './model'

const MAX_ITEMS = 10_000
const MAX_DEPTH = 2_048
const MAX_STRING = 1_000_000

export type ProjectionErrorCode = 'invalid_type' | 'unknown_field' | 'missing_field' | 'limit_exceeded' | 'unknown_variant'

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
type Pending = { value: unknown; path: string; depth: number; assign: (item: BodyItem) => void }

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
  if (value.length > MAX_STRING) fail('limit_exceeded', path, 'string too long')
  return value
}

function strings(value: unknown, path: string): string[] {
  if (!Array.isArray(value)) fail('invalid_type', path, 'expected array')
  if (value.length > MAX_ITEMS) fail('limit_exceeded', path, 'array too long')
  return value.map((item, index) => string(item, `${path}[${index}]`))
}

function result(value: unknown, path: string): CanonicalResult | null {
  if (value === null) return null
  const data = record(value, path)
  exact(data, ['type', 'part_ids', 'text'], path)
  const type = string(data.type, `${path}.type`)
  if (type !== 'ProviderResult' && type !== 'DerivedResult') fail('unknown_variant', `${path}.type`, type)
  return { type, partIds: strings(data.part_ids, `${path}.part_ids`), text: string(data.text, `${path}.text`) }
}

function modelChange(data: RecordValue, path: string): ModelChange {
  exact(data, ['type', 'id', 'before_turn'], path)
  return { type: 'ModelChange', id: string(data.id, `${path}.id`), beforeTurn: string(data.before_turn, `${path}.before_turn`) }
}

function fail(code: ProjectionErrorCode, path: string, message: string): never {
  throw new ProjectionParseError(code, path, message)
}

export function parseProjection(value: unknown): ChatProjection {
  if (!Array.isArray(value)) fail('invalid_type', '$', 'expected array')
  if (value.length > MAX_ITEMS) fail('limit_exceeded', '$', 'too many chat items')
  const output: ChatItem[] = []
  const pending: Pending[] = []
  let nodes = 0

  for (let index = value.length - 1; index >= 0; index -= 1) {
    const data = record(value[index], `$[${index}]`)
    const type = string(data.type, `$[${index}].type`)
    if (type === 'ModelChange') {
      output.unshift(modelChange(data, `$[${index}]`))
      continue
    }
    if (type !== 'Turn') fail('unknown_variant', `$[${index}].type`, type)
    exact(data, ['type', 'id', 'prompt', 'body', 'result'], `$[${index}]`)
    if (!Array.isArray(data.body)) fail('invalid_type', `$[${index}].body`, 'expected array')
    const turn: Turn = { type: 'Turn', id: string(data.id, `$[${index}].id`), prompt: string(data.prompt, `$[${index}].prompt`), body: [], result: result(data.result, `$[${index}].result`) }
    output.unshift(turn)
    pushChildren(pending, data.body, `$[${index}].body`, 1, (item) => turn.body.push(item))
  }

  while (pending.length > 0) {
    const task = pending.pop()!
    if (++nodes > MAX_ITEMS || task.depth > MAX_DEPTH) fail('limit_exceeded', task.path, 'projection nesting limit exceeded')
    const data = record(task.value, task.path)
    const type = string(data.type, `${task.path}.type`)
    if (type === 'Explanation') {
      exact(data, ['type', 'text', 'text_event_ids', 'item_ids'], task.path)
      task.assign({ type, text: string(data.text, `${task.path}.text`), textEventIds: strings(data.text_event_ids, `${task.path}.text_event_ids`), itemIds: strings(data.item_ids, `${task.path}.item_ids`) })
      continue
    }
    if (type === 'SteeringMessage') {
      exact(data, ['type', 'id', 'text'], task.path)
      task.assign({ type, id: string(data.id, `${task.path}.id`), text: string(data.text, `${task.path}.text`) })
      continue
    }
    if (type !== 'NativeSubagentTurn' && type !== 'WorkerTurn') fail('unknown_variant', `${task.path}.type`, type)
    exact(data, ['type', 'id', 'prompt', 'body', 'result', 'children'], task.path)
    if (!Array.isArray(data.body)) fail('invalid_type', `${task.path}.body`, 'expected array')
    const scoped: ScopedTurn = { type, id: string(data.id, `${task.path}.id`), prompt: string(data.prompt, `${task.path}.prompt`), body: [], result: result(data.result, `${task.path}.result`), children: strings(data.children, `${task.path}.children`) }
    task.assign(scoped)
    pushChildren(pending, data.body, `${task.path}.body`, task.depth + 1, (item) => scoped.body.push(item))
  }
  return output
}

function pushChildren(pending: Pending[], body: unknown[], path: string, depth: number, assign: (item: BodyItem) => void): void {
  if (body.length > MAX_ITEMS) fail('limit_exceeded', path, 'array too long')
  const slots = new Array<BodyItem>(body.length)
  if (body.length === 0) return
  pending.push({ value: { type: 'Explanation', text: '', text_event_ids: [], item_ids: [] }, path: `${path}.__flush`, depth, assign: () => { for (const item of slots) assign(item) } })
  for (let index = body.length - 1; index >= 0; index -= 1) {
    pending.push({ value: body[index], path: `${path}[${index}]`, depth, assign: (item) => { slots[index] = item } })
  }
}
