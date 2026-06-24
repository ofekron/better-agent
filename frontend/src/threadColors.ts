/** Thread color palette — visually distinct, dark-theme friendly */
const THREAD_COLORS = [
  "#58a6ff", // blue
  "#f0883e", // orange
  "#3fb950", // green
  "#d2a8ff", // lavender
  "#f778ba", // pink
  "#ffd33d", // yellow
  "#79c0ff", // light blue
  "#ff7b72", // coral
  "#7ee787", // mint
  "#e3b341", // gold
] as const;

const threadColorCache = new Map<string, string>();

/** Get a stable color for a thread id. Same id always returns the same color. */
export function getThreadColor(threadId: string): string {
  if (threadColorCache.has(threadId)) return threadColorCache.get(threadId)!;
  const idx = threadColorCache.size % THREAD_COLORS.length;
  const color = THREAD_COLORS[idx];
  threadColorCache.set(threadId, color);
  return color;
}

/** Build a map of threadId -> color from a list of thread ids (stable ordering) */
export function buildThreadColorMap(threadIds: string[]): Map<string, string> {
  const map = new Map<string, string>();
  threadIds.forEach((id, i) => {
    map.set(id, THREAD_COLORS[i % THREAD_COLORS.length]);
  });
  return map;
}
