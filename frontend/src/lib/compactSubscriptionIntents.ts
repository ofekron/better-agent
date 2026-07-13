export type CompactSubscriptionMode = 'foreground' | 'cache'

export function buildCompactSubscriptionModes(
  foregroundId: string | null,
  visibleForegroundIds: readonly string[],
  warmCacheIds: readonly string[],
): Map<string, CompactSubscriptionMode> {
  const modes = new Map<string, CompactSubscriptionMode>()
  if (foregroundId) modes.set(foregroundId, 'foreground')
  for (const id of visibleForegroundIds) if (id) modes.set(id, 'foreground')
  let warmCount = 0
  for (const id of warmCacheIds) {
    if (!id || modes.has(id) || warmCount >= 19) continue
    modes.set(id, 'cache')
    warmCount += 1
  }
  return modes
}
