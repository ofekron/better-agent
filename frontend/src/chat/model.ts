export type RunMeta = { provider: string; model: string; effort: string }

export type ModelChange = {
  type: 'ModelChange'
  id: string
  beforeTurn: string
}

export type CanonicalResult = {
  type: 'ProviderResult' | 'DerivedResult'
  partIds: string[]
  text: string
}

export type Explanation = {
  type: 'Explanation'
  text: string
  textEventIds: string[]
  itemIds: string[]
}

export type SteeringMessage = {
  type: 'SteeringMessage'
  id: string
  text: string
}

export type ScopedTurn = {
  type: 'NativeSubagentTurn' | 'WorkerTurn'
  id: string
  prompt: string
  body: BodyItem[]
  result: CanonicalResult | null
  children: string[]
}

export type BodyItem = Explanation | SteeringMessage | ScopedTurn

export type Turn = {
  type: 'Turn'
  id: string
  prompt: string
  body: BodyItem[]
  result: CanonicalResult | null
}

export type ChatItem = ModelChange | Turn
export type ChatProjection = readonly ChatItem[]

export type RenderMode = 'collapsed' | 'extended' | 'live'

export type RenderToken =
  | { kind: 'prompt'; id: string }
  | { kind: 'ellipsis'; id: string }
  | { kind: 'result'; ids: string[]; text: string }
  | { kind: 'explanation'; text: string; textEventIds: string[]; itemIds: string[]; itemCount: number; expanded: boolean }
  | { kind: 'steering' | 'native' | 'worker' | 'compact' | 'expanded' | 'internal'; id: string }

export type VisibleEvent = { id: string; scope: string; run: RunMeta }
export type ModelMarker = VisibleEvent

export function assertNever(value: never): never {
  throw new Error(`Unhandled canonical value: ${JSON.stringify(value)}`)
}
