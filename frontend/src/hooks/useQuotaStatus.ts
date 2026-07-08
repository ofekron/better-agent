import { useEffect, useState } from "react";
import { quotaStatusUrl, type QuotaStatus } from "../utils/quotaStatus";

// Same cadence as the usage-gauge extension module.
const REFRESH_INTERVAL_MS = 5 * 60 * 1000;

// Module-level singleton: ONE poller + ONE visibility listener shared by every
// mounted picker, so opening multiple pickers (or RuntimeProfilePicker, which
// mounts twice in team mode) does not multiply outbound quota-status requests.
// The backend further dedups upstream provider calls via its 60s cache.
//
// `apiBase` is app-global — every caller passes the same API constant. The
// poller latches the first api and never repoints on a later differing mount,
// so a stray caller cannot hijack the shared reading.
let cached: QuotaStatus = {};
let currentApi = "";
let pollTimer: number | undefined;
let visibilityBound = false;
const listeners = new Set<(status: QuotaStatus) => void>();
// Monotonic sequence: only the most recently initiated fetch may write, so an
// older in-flight request cannot overwrite `cached` with stale numbers.
let fetchSeq = 0;

async function fetchOnce(api: string): Promise<void> {
  const my = ++fetchSeq;
  try {
    const res = await fetch(quotaStatusUrl(api));
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
  if (!document.hidden) fetchOnce(currentApi);
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
}

function ensurePolling(api: string): void {
  if (pollTimer !== undefined) return; // already polling on the latched api
  currentApi = api;
  fetchOnce(api);
  pollTimer = window.setInterval(() => fetchOnce(currentApi), REFRESH_INTERVAL_MS);
  document.addEventListener("visibilitychange", onVisible);
  visibilityBound = true;
}

/** Subscribes to the shared quota-status poll. The first mounted consumer
 * starts the poller; the last unmounted consumer stops it. Fail-soft. */
export function useQuotaStatus(apiBase: string): QuotaStatus {
  const [status, setStatus] = useState<QuotaStatus>(cached);
  useEffect(() => {
    const emit = (next: QuotaStatus) => setStatus(next);
    listeners.add(emit);
    ensurePolling(apiBase);
    return () => {
      listeners.delete(emit);
      if (listeners.size === 0) stopPolling();
    };
  }, [apiBase]);
  return status;
}
