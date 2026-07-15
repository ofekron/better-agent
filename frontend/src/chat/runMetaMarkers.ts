import { decorateModelRuns } from './decorateModelRuns'
import type { VisibleEvent } from './model'

type RunMetaLike = {
  provider_id?: string | null
  model?: string | null
  reasoning_effort?: string | null
} | null | undefined

type MarkerGroup = {
  responseMessage?: { id: string; run_meta?: RunMetaLike } | null
  isLatest: boolean
}

type SessionRunDefaults = {
  provider_id?: string | null
  model?: string | null
  reasoning_effort?: string | null
} | null | undefined

/** Spec (chat-panel.md): one provider/model/effort marker per contiguous
 * run, on the run's last visible assistant message. Each assistant message
 * is one visible event in the root panel scope; its run identity comes
 * from the message run_meta, falling back to the session's current
 * settings for the latest turn (mirrors AssistantRunMeta's fallback so
 * the marker decision matches what would render). Returns the ids of the
 * messages that end a run. */
export function runMetaMarkedMessageIds(
  groups: readonly MarkerGroup[],
  session: SessionRunDefaults,
): Set<string> {
  const visible: VisibleEvent[] = []
  for (const group of groups) {
    const message = group.responseMessage
    if (!message) continue
    const meta = message.run_meta
    const fallback = group.isLatest ? session : undefined
    visible.push({
      id: message.id,
      scope: 'root',
      run: {
        provider: meta?.provider_id ?? fallback?.provider_id ?? '',
        model: meta?.model ?? fallback?.model ?? '',
        effort: meta?.reasoning_effort ?? fallback?.reasoning_effort ?? '',
      },
    })
  }
  return new Set(decorateModelRuns(visible).map((marker) => marker.id))
}
