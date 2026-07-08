import { useEffect, useRef, useState } from "react";
import type { Provider } from "../types";
import { quotaStatusUrl, type QuotaStatus } from "../utils/quotaStatus";

// Same cadence as the usage-gauge extension module.
const REFRESH_INTERVAL_MS = 5 * 60 * 1000;

// Module-level singleton: ONE poller + ONE visibility listener shared by every
// mounted picker, so opening multiple pickers (or RuntimeProfilePicker, which
// mounts twice in team mode) does not multiply outbound quota-status requests.
// The backend further dedups upstream provider calls via its 60s cache.
//
// Quota is resolved per (kind, config_dir) — the POST body carries the distinct
// account pairs derived from the providers list, and the response is keyed by
// "<kind>::<config_dir>". `apiBase` and the provider-set signature are latched:
// a stray caller or an unchanged set never repoints or re-starts the poller.
let cached: QuotaStatus = {};
let currentApi = "";
let currentSig = "";
let currentPairs: { kind: string; config_dir: string }[] = [];
let pollTimer: number | undefined;
let visibilityBound = false;
const listeners = new Set<(status: QuotaStatus) => void>();
// Monotonic sequence: only the most recently initiated fetch may write, so an
// older in-flight request cannot overwrite `cached` with stale numbers.
let fetchSeq = 0;

/** Distinct supported (kind, config_dir) account pairs to query. The extension
 * measures quota against one CLI token per pair. */
export function distinctQuotaAccounts(
  providers: Provider[],
): { kind: string; config_dir: string }[] {
  const seen = new Set<string>();
  const out: { kind: string; config_dir: string }[] = [];
  for (const p of providers) {
    if (p.suspended) continue;
    if (p.kind !== "claude" && p.kind !== "codex") continue;
    const config_dir = p.config_dir || "";
    const key = `${p.kind}::${config_dir}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ kind: p.kind, config_dir });
  }
  return out;
}

async function fetchOnce(): Promise<void> {
  const my = ++fetchSeq;
  try {
    const res = await fetch(quotaStatusUrl(currentApi), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ providers: currentPairs }),
    });
    if (!res.ok) return;
    const data = await res.json();
    if (data?.providers && my === fetchSeq) {
      cached = data.providers;
      for (const emit of listeners) emit(cached);
    }
  } catch {
    // Best-effort: keep the previous reading; quota is advisory, never blocking.
  }
}

function onVisible(): void {
  if (!document.hidden) fetchOnce();
}

function stopPolling(): void {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer);
    pollTimer = undefined;
  }
  if (visibilityBound) {
    document.removeEventListener("visibilitychange", onVisible);
    visibilityBound = false;
  }
  currentApi = "";
  currentSig = "";
}

function ensurePolling(
  api: string,
  sig: string,
  pairs: { kind: string; config_dir: string }[],
): void {
  if (pollTimer !== undefined && api === currentApi && sig === currentSig) return;
  stopPolling();
  currentApi = api;
  currentSig = sig;
  currentPairs = pairs;
  fetchOnce();
  pollTimer = window.setInterval(fetchOnce, REFRESH_INTERVAL_MS);
  document.addEventListener("visibilitychange", onVisible);
  visibilityBound = true;
}

/** Subscribes to the shared per-provider quota-status poll. The first mounted
 * consumer (or a change in the provider-set signature) starts/repoints the
 * poller; the last unmounted consumer stops it. Fail-soft. */
export function useQuotaStatus(apiBase: string, providers: Provider[]): QuotaStatus {
  const [status, setStatus] = useState<QuotaStatus>(cached);
  const providersRef = useRef(providers);
  providersRef.current = providers;
  const sig = distinctQuotaAccounts(providers)
    .map((p) => `${p.kind}::${p.config_dir}`)
    .join("|");
  useEffect(() => {
    const emit = (next: QuotaStatus) => setStatus(next);
    listeners.add(emit);
    ensurePolling(apiBase, sig, distinctQuotaAccounts(providersRef.current));
    return () => {
      listeners.delete(emit);
      if (listeners.size === 0) stopPolling();
    };
  }, [apiBase, sig]);
  return status;
}
