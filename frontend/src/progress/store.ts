import { useSyncExternalStore } from "react";
import type { WSEvent } from "../types";
import { logFailure, logMutationFailure, logTiming } from "../lib/frontendLogger";
import { fetchWithRetry } from "../utils/fetchRetry";

type OpEntry = {
  count: number;
  error: string | null;
};

export type SyncFailure = {
  operationId: string;
  correlationId: string;
  action: string;
  info: string | null;
  details: string;
};

type SyncEntry = {
  operationId: string;
  correlationId: string;
  action: string;
  status: "pending" | "failed";
  info: string | null;
  details: string;
};

export type ThreeStateSyncOptions<TAuthoritative> = {
  operationId: string;
  action: string;
  expectedAuthoritativeState?: (state: TAuthoritative) => boolean;
  reconcile: () => void | Promise<void>;
  info?: string;
};

export type ThreeStateSyncController<TAuthoritative> = {
  correlationId: string;
  confirmAcknowledgement: () => void;
  observeAuthoritativeState: (state: TAuthoritative) => boolean;
  fail: (error: unknown, details?: string) => Promise<void>;
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
const syncOps = new Map<string, SyncEntry>();
const latestCorrelationByOperation = new Map<string, string>();
const subscribers = new Set<() => void>();
const pendingExtenders = new Map<string, PendingExtender>();
const recentEvents: { ev: WSEvent; ts: number }[] = [];
const RECENT_EVENT_TTL_MS = 2000;

let snapshotVersion = 0;
let cachedSnapshot = ops;
let cachedSyncSnapshot = syncOps;

function notify(): void {
  snapshotVersion++;
  cachedSnapshot = new Map(ops);
  cachedSyncSnapshot = new Map(syncOps);
  for (const s of subscribers) s();
}

function safeVisibleText(value: string | undefined, maxLength = 240): string | null {
  if (!value) return null;
  const printable = Array.from(value, (character) => {
    const code = character.charCodeAt(0);
    return code < 32 || code === 127 ? " " : character;
  }).join("");
  const sanitized = printable
    .replace(/([?&](?:token|access_token|refresh_token|ticket)=)[^&#\s]+/gi, "$1[REDACTED]")
    .replace(/(\bBearer\s+)[A-Za-z0-9._~+/=-]+/gi, "$1[REDACTED]")
    .replace(/\s+/g, " ")
    .trim();
  return sanitized ? sanitized.slice(0, maxLength) : null;
}

function safeFailureDetails(operationId: string, correlationId: string): string {
  return JSON.stringify({
    action_key: mutationActionKey(operationId),
    correlation_id: correlationId,
  }, null, 2);
}

function mutationActionKey(operationId: string): string {
  return operationId.split(":").slice(0, 2).join(".");
}

export function beginThreeStateSync<TAuthoritative>(
  options: ThreeStateSyncOptions<TAuthoritative>,
): ThreeStateSyncController<TAuthoritative> {
  const correlationId = crypto.randomUUID();
  const supersededCorrelation = latestCorrelationByOperation.get(options.operationId);
  if (supersededCorrelation) {
    const superseded = syncOps.get(supersededCorrelation);
    syncOps.delete(supersededCorrelation);
    if (superseded?.status === "pending") completeOp(options.operationId);
    if (superseded?.status === "failed") clearError(options.operationId);
  }
  latestCorrelationByOperation.set(options.operationId, correlationId);
  syncOps.set(correlationId, {
    operationId: options.operationId,
    correlationId,
    action: safeVisibleText(options.action) ?? options.action,
    status: "pending",
    info: safeVisibleText(options.info),
    details: "",
  });
  startOp(options.operationId);

  let settled = false;
  const isCurrent = (): boolean =>
    latestCorrelationByOperation.get(options.operationId) === correlationId;

  const confirmAcknowledgement = (): void => {
    if (settled) return;
    settled = true;
    if (!isCurrent()) return;
    syncOps.delete(correlationId);
    latestCorrelationByOperation.delete(options.operationId);
    completeOp(options.operationId);
  };

  const observeAuthoritativeState = (state: TAuthoritative): boolean => {
    if (settled || !options.expectedAuthoritativeState?.(state)) return false;
    confirmAcknowledgement();
    return true;
  };

  const fail = async (error: unknown): Promise<void> => {
    if (settled) return;
    settled = true;
    const current = isCurrent();
    try {
      await options.reconcile();
    } catch (reconcileError) {
      logFailure("three-state-sync", "reconcile.failed", reconcileError, {
        action_key: mutationActionKey(options.operationId),
        correlation_id: correlationId,
      });
    }
    logMutationFailure({
      actionKey: mutationActionKey(options.operationId),
      correlationId,
      failureKind: error instanceof TypeError ? "network" : error instanceof Error ? "rejected" : "unknown",
    });
    if (!current) return;
    syncOps.set(correlationId, {
      operationId: options.operationId,
      correlationId,
      action: safeVisibleText(options.action) ?? options.action,
      status: "failed",
      info: safeVisibleText(options.info),
      details: safeFailureDetails(options.operationId, correlationId),
    });
    failOp(options.operationId, safeVisibleText(options.info) ?? options.action);
  };

  return { correlationId, confirmAcknowledgement, observeAuthoritativeState, fail };
}

export function runThreeStateSync<TResult, TAuthoritative>(
  options: ThreeStateSyncOptions<TAuthoritative> & {
    mutate: () => Promise<TResult>;
    isAcknowledged?: (result: TResult) => boolean;
  },
): Promise<{ result: TResult; controller: ThreeStateSyncController<TAuthoritative> }> & {
  controller: ThreeStateSyncController<TAuthoritative>;
} {
  const controller = beginThreeStateSync(options);
  const promise = options.mutate().then(
    (result) => {
      const acknowledged = options.isAcknowledged?.(result);
      if (!options.expectedAuthoritativeState && (acknowledged === true || acknowledged === undefined)) {
        controller.confirmAcknowledgement();
      }
      return { result, controller };
    },
    async (error) => {
      await controller.fail(error);
      throw error;
    },
  );
  return Object.assign(promise, { controller });
}

export function dismissSyncFailure(correlationId: string): void {
  const entry = syncOps.get(correlationId);
  if (!entry || entry.status !== "failed") return;
  syncOps.delete(correlationId);
  if (latestCorrelationByOperation.get(entry.operationId) === correlationId) {
    latestCorrelationByOperation.delete(entry.operationId);
  }
  clearError(entry.operationId);
}

type SyncStatusSnapshot = { pendingCount: number; failures: readonly SyncFailure[] };
const EMPTY_SYNC_STATUS: SyncStatusSnapshot = { pendingCount: 0, failures: [] };

export function useSyncStatus(): SyncStatusSnapshot {
  const map = useSyncExternalStore(subscribe, () => cachedSyncSnapshot, () => cachedSyncSnapshot);
  let pendingCount = 0;
  const failures: SyncFailure[] = [];
  for (const entry of map.values()) {
    if (entry.status === "pending") pendingCount += 1;
    if (entry.status === "failed") failures.push(entry);
  }
  if (pendingCount === 0 && failures.length === 0) return EMPTY_SYNC_STATUS;
  return { pendingCount, failures };
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
