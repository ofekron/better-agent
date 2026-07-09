export function additionalSessionSubscriptionIds(
  openSessionIds: string[],
  primarySubscriptionId: string | null | undefined,
): string[] {
  const ids = new Set<string>();
  for (const id of openSessionIds) {
    if (!id || id === primarySubscriptionId) continue;
    ids.add(id);
  }
  return Array.from(ids);
}
