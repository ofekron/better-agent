import { logDurable } from "./frontendLogger";

type MetricValue = number | boolean | string | null;
const enabled = () =>
  typeof window !== "undefined"
  && !(typeof process !== "undefined" && process.env.VITEST && process.env.BA_RENDER_PROFILER_IN_TESTS !== "1")
  && new URLSearchParams(window.location.search).get("ba_perf") === "1";

export function perfId(value?: string): string {
  if (!value) return "none";
  let hash = 2166136261;
  for (let i = 0; i < value.length; i++) hash = Math.imul(hash ^ value.charCodeAt(i), 16777619);
  return (hash >>> 0).toString(36);
}

export function perfRecord(stage: string, metrics: Record<string, MetricValue>): void {
  if (!enabled()) return;
  const safe = { ...metrics, viewport: window.innerWidth < 600 ? "compact" : "wide" };
  performance.mark(`ba:${stage}`, { detail: safe });
  logDurable("render-perf", stage, safe);
}

export function perfSpan(stage: string, metrics: Record<string, MetricValue> = {}): () => number {
  if (!enabled()) return () => 0;
  const startedAt = performance.now();
  return () => {
    const duration = performance.now() - startedAt;
    perfRecord(stage, { ...metrics, duration_ms: Math.round(duration * 10) / 10 });
    return duration;
  };
}
