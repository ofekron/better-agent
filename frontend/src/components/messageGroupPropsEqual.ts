/**
 * Comparator for `memo(MessageGroup)`: shallow-compare every prop by
 * reference (like default memo) but content-compare `runs`.
 *
 * A streaming token rebuilds Chat.tsx's `groups` useMemo (its `allMessages`
 * dep churns every token), which mints a fresh `runs` array for each
 * run-bearing group even when the run set is unchanged. Default memo's
 * reference compare would then re-render every run-bearing group — and its
 * AssistantMessage subtree — on every token. `visibleRuns` is
 * reference-stable while streaming, so equal-content arrays share element
 * references and the element-wise compare holds. Can never go stale: a real
 * change to the run set changes the length or an element ref.
 */
export function messageGroupPropsEqual<T extends object>(
  prev: T,
  next: T,
): boolean {
  for (const key of Object.keys(prev)) {
    const a = (prev as Record<string, unknown>)[key];
    const b = (next as Record<string, unknown>)[key];
    if (a === b) continue;
    if (
      key === "runs" &&
      Array.isArray(a) &&
      Array.isArray(b) &&
      a.length === b.length &&
      (a as unknown[]).every((v, i) => v === (b as unknown[])[i])
    ) {
      continue;
    }
    return false;
  }
  return true;
}
