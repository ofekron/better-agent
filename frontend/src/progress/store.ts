import { useSyncExternalStore } from "react";
import type { WSEvent } from "../types";
import { logFailure, logTiming } from "../lib/frontendLogger";
import { fetchWithRetry } from "../utils/fetchRetry";

type OpEntry = {
  count: number;
  error: string | null;
};

type PendingExtender = {
  opId: string;
  predicate: (ev: WSEvent) => boolean;
  onMatch: () => void;
};

type TrackOpts = {
  silent?: boolean;
};

type TrackHandle<T> = {
  promise: Promise<T>;
  armWSExtender: (predicate: (ev: WSEvent) => boolean) => void;
};

const ops = new Map<string, OpEntry>();
const subscribers = new Set<() => void>();
const pendingExtenders = new Map<string, PendingExtender>();
const recentEvents: { ev: WSEvent; ts: number }[] = [];
const RECENT_EVENT_TTL_MS = 2000;

let snapshotVersion = 0;
let cachedSnapshot = ops;

function notify(): void {
  snapshotVersion++;
  cachedSnapshot = new Map(ops);
  for (const s of subscribers) s();
}

function pruneRecent(now: number): void {
  while (recentEvents.length > 0 && now - recentEvents[0].ts > RECENT_EVENT_TTL_MS) {
    recentEvents.shift();
  }
}

export function startOp(opId: string): void {
  const cur = ops.get(opId);
  if (cur) {
    cur.count += 1;
    cur.error = null;
  } else {
    ops.set(opId, { count: 1, error: null });
  }
  notify();
}

export function completeOp(opId: string): void {
  const cur = ops.get(opId);
  if (!cur) return;
  cur.count = Math.max(0, cur.count - 1);
  if (cur.count === 0 && !cur.error) {
    ops.delete(opId);
  }
  notify();
}

export function failOp(opId: string, message: string): void {
  const cur = ops.get(opId);
  if (!cur) {
    ops.set(opId, { count: 0, error: message });
  } else {
    cur.count = Math.max(0, cur.count - 1);
    cur.error = message;
  }
  notify();
}

export function clearError(opId: string): void {
  const cur = ops.get(opId);
  if (!cur || !cur.error) return;
  cur.error = null;
  if (cur.count === 0) ops.delete(opId);
  notify();
}

export function trackPromise<T>(
  opId: string,
  fn: () => Promise<T>,
  opts?: TrackOpts,
): TrackHandle<T> {
  const silent = !!opts?.silent;
  if (!silent) startOp(opId);
  let restDone = false;
  let wsDone = true;
  let armedFn: (() => void) | null = null;

  const tryComplete = (): void => {
    if (silent) return;
    if (restDone && wsDone) {
      completeOp(opId);
      armedFn = null;
    }
  };

  const armWSExtender = (predicate: (ev: WSEvent) => boolean): void => {
    if (silent) return;
    wsDone = false;
    const now = Date.now();
    pruneRecent(now);
    for (const { ev } of recentEvents) {
      if (predicate(ev)) {
        wsDone = true;
        tryComplete();
        return;
      }
    }
    const entry: PendingExtender = {
      opId,
      predicate,
      onMatch: () => {
        wsDone = true;
        tryComplete();
      },
    };
    armedFn = entry.onMatch;
    pendingExtenders.set(opId + ":" + snapshotVersion, entry);
  };

  // Attach side-effects as observation handlers on fn()'s promise
  // directly — do NOT wrap in another async function. Wrapping adds a
  // microtask tick that shifts resolution timing for callers that
  // chain off the returned promise (tests with tight flush() loops
  // depend on the original fetch-level timing). The returned promise
  // is identical to fn()'s; the side-effect chain is fire-and-forget.
  const startedAt = performance.now();
  const promise = fn();
  promise.then(
    () => {
      logTiming("progress", opId, startedAt);
      restDone = true;
      tryComplete();
    },
    (e) => {
      logFailure("progress", `${opId}.failed`, e, {
        duration_ms: Math.round(performance.now() - startedAt),
      });
      restDone = true;
      const msg = e instanceof Error ? e.message : String(e);
      if (!silent) failOp(opId, msg);
      if (armedFn) {
        for (const [k, v] of pendingExtenders) {
          if (v.onMatch === armedFn) pendingExtenders.delete(k);
        }
        armedFn = null;
      }
    },
  );

  return { promise, armWSExtender };
}

export function handleWSEvent(ev: WSEvent): void {
  const now = Date.now();
  recentEvents.push({ ev, ts: now });
  pruneRecent(now);
  if (pendingExtenders.size === 0) return;
  const matched: string[] = [];
  for (const [key, entry] of pendingExtenders) {
    if (entry.predicate(ev)) {
      matched.push(key);
      entry.onMatch();
    }
  }
  for (const key of matched) pendingExtenders.delete(key);
}

function subscribe(fn: () => void): () => void {
  subscribers.add(fn);
  return () => {
    subscribers.delete(fn);
  };
}

function getSnapshot(): Map<string, OpEntry> {
  return cachedSnapshot;
}

export type OpProgress = { inflight: boolean; error: string | null };

const EMPTY: OpProgress = { inflight: false, error: null };

export function useOpProgress(opId: string | readonly string[]): OpProgress {
  const map = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  const ids = typeof opId === "string" ? [opId] : opId;
  let inflight = false;
  let error: string | null = null;
  for (const id of ids) {
    const e = map.get(id);
    if (!e) continue;
    if (e.count > 0) inflight = true;
    if (e.error && !error) error = e.error;
  }
  if (!inflight && !error) return EMPTY;
  return { inflight, error };
}

export function isInflight(opId: string): boolean {
  return (cachedSnapshot.get(opId)?.count ?? 0) > 0;
}

/** Generic fetch wrapper. Tracks the op, retries transient errors
 * (network failures, 5xx, 429), throws on non-2xx after retries
 * exhausted. Returns the response — caller handles JSON/text. */
export async function trackedFetch(
  opId: string,
  input: RequestInfo | URL,
  init?: RequestInit,
  opts?: TrackOpts,
): Promise<Response> {
  const { promise } = trackPromise(
    opId,
    async () => {
      const r = await fetchWithRetry(input, init);
      if (!r.ok) {
        const body = await r.text().catch(() => "");
        throw new Error(body || `HTTP ${r.status}`);
      }
      return r;
    },
    opts,
  );
  return promise;
}
