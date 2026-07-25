import { useEffect, useState } from "react";
import type { Provider } from "../types";
import {
  quotaStatusUrl,
  type QuotaProviderStatus,
  type QuotaStatus,
} from "../utils/quotaStatus";

// Bounds OUTBOUND traffic, not freshness: the backend joins concurrent cache
// misses and refetches upstream on a miss, so each poll returns a reading
// taken at poll time.
const REFRESH_INTERVAL_MS = 5 * 60 * 1000;
// A `loading` provider means the backend is still fetching its first reading
// and exceeded its own join deadline. Re-poll a bounded number of times so
// the numbers land seconds later instead of at the next interval tick — the
// whole reason quota used to stay blank for minutes after a cold start.
const LOADING_RETRY_MS = 4000;
const LOADING_RETRY_LIMIT = 3;

type Emit = (status: QuotaStatus) => void;

// Module-level singleton: ONE poller + ONE visibility listener shared by every
// mounted consumer, so opening multiple pickers (or RuntimeProfilePicker, which
// mounts twice in team mode) does not multiply outbound quota-status requests.
//
// Quota is resolved per provider entry — the POST body carries the UNION of
// every subscriber's entries, because subscribers legitimately watch different
// provider subsets (team mode pairs a manager-capable subset with the full
// list) and a subset-only request would resolve the missing providers to
// `undefined` for everyone.
let cached: QuotaStatus = {};
let currentApi = "";
let currentSig = "";
let currentEntries: QuotaAccountEntry[] = [];
let pollTimer: number | undefined;
let retryTimer: number | undefined;
let retriesLeft = 0;
// Set once the retry budget is spent without a usable response, so the UI can
// say "cannot read quota" instead of implying the providers have none.
let unreachable = false;
let fetchInFlight = false;
let lastFetchStartedAt = 0;
let visibilityBound = false;
const subscribers = new Map<Emit, QuotaAccountEntry[]>();
// Monotonic sequence: only the most recently initiated fetch may write, so an
// older in-flight request cannot overwrite `cached` with stale numbers.
let fetchSeq = 0;

export interface QuotaAccountEntry {
  id: string;
  kind: string;
  mode: string;
  base_url: string;
  config_dir: string;
  name: string;
}

/** Active provider entries to query. The extension routes each entry to the
 * account whose quota it consumes and marks unsupported ones explicitly. */
export function distinctQuotaAccounts(providers: Provider[]): QuotaAccountEntry[] {
  const out: QuotaAccountEntry[] = [];
  for (const p of providers) {
    if (p.suspended) continue;
    // The backend drops kind-less entries, so sending them would guarantee a
    // permanently missing key in the response.
    if (!(p.kind || "").trim()) continue;
    out.push({
      id: (p.id || "").trim(),
      kind: p.kind.trim(),
      mode: p.mode,
      base_url: p.base_url || "",
      config_dir: (p.config_dir || "").trim(),
      name: p.name,
    });
  }
  return out;
}

const entryKey = (e: QuotaAccountEntry): string =>
  `${e.id}:${e.kind}:${e.mode}:${e.base_url}:${e.config_dir}`;

/** Response key the backend uses for an entry. Mirrors providerQuotaKey AND
 * the route's own trimming — a mismatch silently drops a queried provider's
 * reading, which renders as "no usage" forever. */
const responseKey = (e: QuotaAccountEntry): string =>
  e.id || `${e.kind}::${e.config_dir}`;

function unionEntries(): QuotaAccountEntry[] {
  const byKey = new Map<string, QuotaAccountEntry>();
  for (const entries of subscribers.values()) {
    for (const entry of entries) byKey.set(entryKey(entry), entry);
  }
  return [...byKey.values()].sort((a, b) => entryKey(a).localeCompare(entryKey(b)));
}

const signatureOf = (entries: QuotaAccountEntry[]): string =>
  entries.map(entryKey).join("|");

/** A queried provider the backend has not answered for YET. Without this the
 * pre-first-response window (host spawn, in-flight POST) and an unreachable
 * extension both rendered as a definitive "No usage yet" — the exact wrong
 * signal, since the truth is "not known yet" / "cannot be read". */
function pendingFrame(entry: QuotaAccountEntry): QuotaProviderStatus {
  const base = {
    provider: entry.kind,
    label: entry.name || entry.kind,
    supported: true,
    windows: [],
  };
  return unreachable
    ? { ...base, error: "extension_unreachable" }
    : { ...base, loading: true };
}

/** Backend readings, with a pending/unreachable frame filled in for every
 * queried entry the response has not covered. */
function project(entries: QuotaAccountEntry[]): QuotaStatus {
  const out: QuotaStatus = { ...cached };
  for (const entry of entries) {
    const key = responseKey(entry);
    if (!out[key]) out[key] = pendingFrame(entry);
  }
  return out;
}

function emitProjection(): void {
  const next = project(currentEntries);
  for (const emit of subscribers.keys()) emit(next);
}

/** True when the backend says a read is in flight and has no numbers to show
 * for it yet — `loading` (nothing cached at all) or `refreshing` behind an
 * error with no windows. Both mean "the answer is seconds away", so re-poll
 * rather than sit on a wrong empty/error state until the next interval. A
 * `refreshing` reading that DOES carry windows is already displayable and
 * must not trigger a retry, or every poll would re-poll forever. */
function awaitingNumbers(status: QuotaProviderStatus | undefined): boolean {
  if (!status) return false;
  if (status.loading) return true;
  return status.refreshing === true && !(status.windows?.length ?? 0);
}

/** Bounded re-poll. The counter, not the response, guarantees termination. */
function scheduleRetry(): void {
  if (retryTimer !== undefined || subscribers.size === 0) return;
  if (retriesLeft <= 0) {
    // Budget spent with nothing usable: say so instead of implying no quota.
    if (!Object.keys(cached).length && !unreachable) {
      unreachable = true;
      emitProjection();
    }
    return;
  }
  retriesLeft -= 1;
  retryTimer = window.setTimeout(() => {
    retryTimer = undefined;
    void fetchOnce();
  }, LOADING_RETRY_MS);
}

async function fetchOnce(): Promise<void> {
  const my = ++fetchSeq;
  fetchInFlight = true;
  lastFetchStartedAt = Date.now();
  try {
    const res = await fetch(quotaStatusUrl(currentApi), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ providers: currentEntries }),
    });
    if (!res.ok) {
      // Includes 503 while the extension is disabled/quarantined. Retry a
      // bounded number of times rather than silently showing nothing.
      scheduleRetry();
      return;
    }
    const data = await res.json();
    if (!data?.providers || my !== fetchSeq) return;
    cached = data.providers;
    unreachable = false;
    emitProjection();
    if (Object.values(cached).some(awaitingNumbers)) scheduleRetry();
  } catch {
    // Best-effort: keep the previous reading; quota is advisory, never blocking.
    scheduleRetry();
  } finally {
    if (my === fetchSeq) fetchInFlight = false;
  }
}

// Refocus is a user action, so it may repeat arbitrarily fast. Coalesce it:
// the server caches for 60s, so back-to-back refetches are pure waste and
// push the handler toward the host's slow-call ceiling.
const VISIBILITY_REFETCH_MIN_GAP_MS = 2000;

function onVisible(): void {
  if (document.hidden || fetchInFlight) return;
  if (Date.now() - lastFetchStartedAt < VISIBILITY_REFETCH_MIN_GAP_MS) return;
  retriesLeft = LOADING_RETRY_LIMIT;
  void fetchOnce();
}

function stopPolling(): void {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer);
    pollTimer = undefined;
  }
  if (retryTimer !== undefined) {
    window.clearTimeout(retryTimer);
    retryTimer = undefined;
  }
  if (visibilityBound) {
    document.removeEventListener("visibilitychange", onVisible);
    visibilityBound = false;
  }
  currentApi = "";
  currentSig = "";
}

/** Points the shared poller at the union of every subscriber's entries,
 * restarting it only when the api base or that union actually changes. */
function reconcile(api: string): void {
  if (subscribers.size === 0) {
    stopPolling();
    return;
  }
  const entries = unionEntries();
  const sig = signatureOf(entries);
  if (pollTimer !== undefined && api === currentApi && sig === currentSig) return;
  stopPolling();
  currentApi = api;
  currentSig = sig;
  currentEntries = entries;
  // Drop readings for providers no longer queried: rendering them would
  // present data the backend was not asked about as if it were current.
  const allowed = new Set(entries.map(responseKey));
  const kept = Object.entries(cached).filter(([key]) => allowed.has(key));
  if (kept.length !== Object.keys(cached).length) cached = Object.fromEntries(kept);
  retriesLeft = LOADING_RETRY_LIMIT;
  unreachable = false;
  // Publish the pending projection BEFORE awaiting the network, so newly
  // queried providers show progress instead of an empty state.
  emitProjection();
  void fetchOnce();
  pollTimer = window.setInterval(() => {
    retriesLeft = LOADING_RETRY_LIMIT;
    void fetchOnce();
  }, REFRESH_INTERVAL_MS);
  document.addEventListener("visibilitychange", onVisible);
  visibilityBound = true;
}

/** Subscribes to the shared per-provider quota-status poll. Every subscriber's
 * provider set is part of the queried union, so a narrow consumer can never
 * starve a wider one. Fail-soft. */
export function useQuotaStatus(apiBase: string, providers: Provider[]): QuotaStatus {
  const entries = distinctQuotaAccounts(providers);
  // Seeded with the pending projection so the first render — before the
  // subscribe effect has even run — already shows progress rather than an
  // empty state for providers no reading exists for yet.
  const [status, setStatus] = useState<QuotaStatus>(() => project(entries));
  // One stable emit identity per consumer — it doubles as this subscriber's
  // key in the shared registry.
  const [emit] = useState<Emit>(() => (next: QuotaStatus) => setStatus(next));
  const sig = signatureOf(entries);
  useEffect(() => {
    subscribers.set(emit, entries);
    reconcile(apiBase);
    return () => {
      subscribers.delete(emit);
      reconcile(apiBase);
    };
    // `entries` is fully determined by `sig`; depending on the array's
    // identity would re-subscribe on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBase, sig, emit]);
  return status;
}
