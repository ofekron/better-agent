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

/** Build a map of threadId -> color from a list of thread ids (stable ordering) */
export function buildThreadColorMap(threadIds: string[]): Map<string, string> {
  const map = new Map<string, string>();
  threadIds.forEach((id, i) => {
    map.set(id, THREAD_COLORS[i % THREAD_COLORS.length]);
  });
  return map;
}
