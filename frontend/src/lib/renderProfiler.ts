import { logDurable } from "./frontendLogger";

type MetricValue = number | boolean | string | null;
type Metrics = Record<string, MetricValue>;
interface RenderSample {
  stage: string;
  startedAt: number;
  finishedAt: number;
  metrics: Metrics;
}
interface StageAggregate {
  count: number;
  durationTotal: number;
  durationMax: number;
  maxMetrics: Metrics;
}
interface EvictionEvidence {
  generation: number;
  count: number;
  earliestStartedAt: number;
  latestFinishedAt: number;
}

const PERFORMANCE_INCIDENT_EVENT = "better-agent:performance-incident";
const INCIDENT_WINDOW_MS = 10_000;
const INCIDENT_FLUSH_MS = 1_000;
const MAX_RECENT_SAMPLES = 256;
const MAX_AGGREGATE_STAGES = 32;
const OVERLAP_SLOP_MS = 8;
const RUNTIME_KEY = Symbol.for("better-agent.render-profiler.runtime");

let incidentUntil = 0;
let incidentFlushTimer = 0;
let evictionGeneration = 0;
let evictions: EvictionEvidence | null = null;
const recentSamples: RenderSample[] = [];
const afterIncident = new Map<string, StageAggregate>();

const testProfilingEnabled = () =>
  !(typeof process !== "undefined" && process.env.VITEST && process.env.BA_RENDER_PROFILER_IN_TESTS !== "1");

const explicitlyEnabled = () =>
  typeof window !== "undefined"
  && testProfilingEnabled()
  && new URLSearchParams(window.location.search).get("ba_perf") === "1";

function safeMetrics(metrics: Metrics): Metrics {
  return { ...metrics, viewport: window.innerWidth < 600 ? "compact" : "wide" };
}

function durationOf(sample: RenderSample): number {
  const reported = sample.metrics.duration_ms;
  return typeof reported === "number" ? reported : Math.max(0, sample.finishedAt - sample.startedAt);
}

function addAggregate(target: Map<string, StageAggregate>, sample: RenderSample): void {
  const duration = durationOf(sample);
  const current = target.get(sample.stage);
  if (current) {
    current.count += 1;
    current.durationTotal += duration;
    if (duration >= current.durationMax) {
      current.durationMax = duration;
      current.maxMetrics = sample.metrics;
    }
    return;
  }
  if (target.size >= MAX_AGGREGATE_STAGES) return;
  target.set(sample.stage, {
    count: 1,
    durationTotal: duration,
    durationMax: duration,
    maxMetrics: sample.metrics,
  });
}

function emitAggregate(
  stage: string,
  aggregate: StageAggregate,
  phase: "causal" | "after",
  coverage?: { evidenceTruncated: boolean; droppedSampleCount: number; generation: number },
): void {
  logDurable("render-perf", stage, {
    ...aggregate.maxMetrics,
    samples: aggregate.count,
    total_duration_ms: Math.round(aggregate.durationTotal * 10) / 10,
    max_duration_ms: Math.round(aggregate.durationMax * 10) / 10,
    phase,
    ...(coverage ? {
      evidence_truncated: coverage.evidenceTruncated,
      dropped_sample_count: coverage.droppedSampleCount,
      evidence_generation: coverage.generation,
    } : {}),
  });
}

function flushAfterIncident(): void {
  incidentFlushTimer = 0;
  for (const [stage, aggregate] of afterIncident) emitAggregate(stage, aggregate, "after");
  afterIncident.clear();
}

function scheduleAfterFlush(): void {
  if (incidentFlushTimer) return;
  incidentFlushTimer = window.setTimeout(flushAfterIncident, INCIDENT_FLUSH_MS);
}

function remember(sample: RenderSample): void {
  recentSamples.push(sample);
  if (recentSamples.length > MAX_RECENT_SAMPLES) {
    const removed = recentSamples.splice(0, recentSamples.length - MAX_RECENT_SAMPLES);
    for (const evicted of removed) {
      if (!evictions) {
        evictions = {
          generation: evictionGeneration,
          count: 1,
          earliestStartedAt: evicted.startedAt,
          latestFinishedAt: evicted.finishedAt,
        };
        continue;
      }
      evictions.count += 1;
      evictions.earliestStartedAt = Math.min(evictions.earliestStartedAt, evicted.startedAt);
      evictions.latestFinishedAt = Math.max(evictions.latestFinishedAt, evicted.finishedAt);
    }
  }
  if (sample.finishedAt < incidentUntil) {
    addAggregate(afterIncident, sample);
    scheduleAfterFlush();
  }
}

function flushCausal(startTime: number, durationMs: number): void {
  const endTime = startTime + durationMs;
  const causal = new Map<string, StageAggregate>();
  for (const sample of recentSamples) {
    if (sample.startedAt > endTime + OVERLAP_SLOP_MS) continue;
    if (sample.finishedAt < startTime - OVERLAP_SLOP_MS) continue;
    addAggregate(causal, sample);
  }
  if (evictions && startTime - OVERLAP_SLOP_MS > evictions.latestFinishedAt) {
    evictionGeneration += 1;
    evictions = null;
    const firstRelevant = recentSamples.findIndex(
      (sample) => sample.finishedAt >= startTime - OVERLAP_SLOP_MS,
    );
    if (firstRelevant === -1) recentSamples.length = 0;
    else if (firstRelevant > 0) recentSamples.splice(0, firstRelevant);
  }
  const evictedWindowOverlaps = !!evictions
    && evictions.earliestStartedAt <= endTime + OVERLAP_SLOP_MS
    && evictions.latestFinishedAt >= startTime - OVERLAP_SLOP_MS;
  const coverage = {
    evidenceTruncated: evictedWindowOverlaps,
    droppedSampleCount: evictedWindowOverlaps ? evictions?.count ?? 0 : 0,
    generation: evictionGeneration,
  };
  for (const [stage, aggregate] of causal) emitAggregate(stage, aggregate, "causal", coverage);
  if (causal.size === 0 && coverage.evidenceTruncated) {
    emitAggregate("incident_coverage", {
      count: 0,
      durationTotal: 0,
      durationMax: 0,
      maxMetrics: {},
    }, "causal", coverage);
  }
}

function onPerformanceIncident(event: Event): void {
  if (!testProfilingEnabled()) return;
  const detail = event instanceof CustomEvent ? event.detail as Record<string, unknown> | null : null;
  const startTime = typeof detail?.start_time === "number" ? detail.start_time : NaN;
  const durationMs = typeof detail?.duration_ms === "number" ? detail.duration_ms : NaN;
  if (!Number.isFinite(startTime) || !Number.isFinite(durationMs) || durationMs < 80) return;
  flushCausal(startTime, durationMs);
  incidentUntil = Math.max(incidentUntil, performance.now() + INCIDENT_WINDOW_MS);
}

function disposeRuntime(): void {
  window.removeEventListener(PERFORMANCE_INCIDENT_EVENT, onPerformanceIncident);
  if (incidentFlushTimer) window.clearTimeout(incidentFlushTimer);
  incidentFlushTimer = 0;
  incidentUntil = 0;
  evictionGeneration = 0;
  evictions = null;
  recentSamples.length = 0;
  afterIncident.clear();
}

if (typeof window !== "undefined") {
  const runtime = window as typeof window & { [RUNTIME_KEY]?: () => void };
  runtime[RUNTIME_KEY]?.();
  runtime[RUNTIME_KEY] = disposeRuntime;
  window.addEventListener(PERFORMANCE_INCIDENT_EVENT, onPerformanceIncident);
  import.meta.hot?.dispose(() => {
    disposeRuntime();
    if (runtime[RUNTIME_KEY] === disposeRuntime) delete runtime[RUNTIME_KEY];
  });
}

export function perfId(value?: string): string {
  if (!value) return "none";
  let hash = 2166136261;
  for (let i = 0; i < value.length; i++) hash = Math.imul(hash ^ value.charCodeAt(i), 16777619);
  return (hash >>> 0).toString(36);
}

export function perfRecord(stage: string, metrics: Metrics): void {
  if (!testProfilingEnabled()) return;
  const now = performance.now();
  const safe = safeMetrics(metrics);
  const duration = typeof safe.duration_ms === "number" ? safe.duration_ms : 0;
  remember({ stage, startedAt: now - duration, finishedAt: now, metrics: safe });
  if (!explicitlyEnabled()) return;
  performance.mark(`ba:${stage}`, { detail: safe });
  logDurable("render-perf", stage, safe);
}

export function perfSpan(stage: string, metrics: Metrics = {}): () => number {
  if (!testProfilingEnabled()) return () => 0;
  const startedAt = performance.now();
  return () => {
    const finishedAt = performance.now();
    const duration = finishedAt - startedAt;
    const safe = safeMetrics({ ...metrics, duration_ms: Math.round(duration * 10) / 10 });
    remember({ stage, startedAt, finishedAt, metrics: safe });
    if (explicitlyEnabled()) {
      performance.mark(`ba:${stage}`, { detail: safe });
      logDurable("render-perf", stage, safe);
    }
    return duration;
  };
}
