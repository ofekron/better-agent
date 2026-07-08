// Durable write-through queue for backend-owned UI state.
//
// The backend is the single source of truth for UI state (panel tabs, pins,
// sort/visibility prefs). Every frontend mutation writes through to its
// REST endpoint immediately. When the backend is unreachable, writes are
// collapsed by key into a localStorage-backed backlog and drained on
// reconnect — so offline intent is never silently lost.
//
// This is a TRANSPORT QUEUE, never a second source of truth. The backend
// snapshot (REST on mount + WS push) remains authoritative; the backlog only
// holds writes the backend has not yet acknowledged (HTTP 2xx). It plays the
// same role the old localStorage↔backend mount-union used to: survive
// offline writes — but without polluting the backend with stale cache on
// every page load.

import { API } from "../api";

export type WriteMethod = "PATCH" | "PUT" | "POST";

export interface QueuedWrite {
  method: WriteMethod;
  url: string;
  body: unknown;
  // Collapse key (take_latest): a newer write with the same key replaces a
  // still-pending older one. Scope it so independent fields don't clobber
  // each other (e.g. one key per pref field, one per pin target).
  key: string;
}

const BACKLOG_KEY = "better-agent-write-backlog";

let backlog: QueuedWrite[] = loadBacklog();
let inflight: Promise<void> | null = null;

function loadBacklog(): QueuedWrite[] {
  try {
    const raw = localStorage.getItem(BACKLOG_KEY);
    const parsed = raw ? (JSON.parse(raw) as QueuedWrite[]) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function persistBacklog(): void {
  try {
    localStorage.setItem(BACKLOG_KEY, JSON.stringify(backlog));
  } catch {
    // localStorage unavailable or full — the in-memory queue still drains
    // this session; it just won't survive a reload.
  }
}

async function sweep(): Promise<void> {
  // Snapshot the pending writes BY IDENTITY. A write may be collapsed (replaced
  // by a newer same-key write) while its fetch is in flight; tracking identity
  // means a succeeded fetch only drops the exact object sent, leaving a newer
  // same-key replacement for the next pass.
  const pending = [...backlog];
  const succeeded = new Set<QueuedWrite>();
  for (const w of pending) {
    try {
      const res = await fetch(`${API}${w.url}`, {
        method: w.method,
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(w.body),
      });
      // 2xx = acknowledged (drop); 4xx = client error, retrying won't help
      // (drop); anything else (5xx) stays for retry.
      if (res.ok || (res.status >= 400 && res.status < 500)) succeeded.add(w);
    } catch {
      // Network failure / backend unreachable — leave for retry.
    }
  }
  backlog = backlog.filter((w) => !succeeded.has(w));
  persistBacklog();
  // If new writes arrived during the sweep (not in this snapshot), sweep
  // again so they aren't left waiting for the next reconnect.
  if (backlog.some((w) => !pending.includes(w))) {
    return sweep();
  }
}

/** Queue a write-through. Collapses any pending write sharing the same key,
 * then kicks off (or joins) the in-flight sweep. */
export function queueWrite(write: QueuedWrite): void {
  backlog = backlog.filter((w) => w.key !== write.key);
  backlog.push(write);
  persistBacklog();
  void flushWriteBacklog();
}

/** Attempt every pending write until the backlog drains. Concurrent callers
 * share one sweep. */
export async function flushWriteBacklog(): Promise<void> {
  if (inflight) return inflight;
  inflight = sweep().finally(() => {
    inflight = null;
  });
  return inflight;
}

/** Signal that the backend is reachable again — drain the backlog. Wire to
 * the WS `connected` rising edge (and call once after auth) so writes that
 * failed during a prior offline session (persisted across reload) are
 * re-sent. */
export function signalReconnect(): void {
  void flushWriteBacklog();
}
