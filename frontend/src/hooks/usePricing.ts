import { useCallback, useSyncExternalStore } from "react";
import {
  pricingUrl,
  priceKey,
  type Pricing,
  type PricingRequest,
} from "../utils/pricing";

// One shared price poll for every consumer. The extension refreshes its
// table from upstream once a day, so the client only needs to re-read
// often enough to pick that up plus any newly requested provider x model.
const REFRESH_INTERVAL_MS = 6 * 60 * 60 * 1000;
// The table is still being fetched upstream on a cold start; re-read soon
// rather than leaving cost blank until the next 6h tick.
const LOADING_RETRY_MS = 5000;
const LOADING_RETRY_LIMIT = 4;

type Emit = () => void;

const getSnapshot = (): Pricing => cached;

let cached: Pricing = { prices: {}, meta: null, unavailable: false };
let currentApi = "";
let currentSig = "";
let currentRequests: PricingRequest[] = [];
let pollTimer: number | undefined;
let retryTimer: number | undefined;
let retriesLeft = 0;
let fetchSeq = 0;
const subscribers = new Map<Emit, PricingRequest[]>();

const requestSig = (requests: PricingRequest[]): string =>
  requests
    .map((r) => `${r.provider_id || ""}|${r.kind}|${r.base_url || ""}|${r.model}`)
    .sort()
    .join(",");

/** Union of every subscriber's requests — consumers legitimately watch
 * different provider x model sets, and a subset-only request would resolve
 * the missing ones to undefined for everyone. */
function unionRequests(): PricingRequest[] {
  const byKey = new Map<string, PricingRequest>();
  for (const requests of subscribers.values()) {
    for (const request of requests) {
      if (!request.kind || !request.model) continue;
      byKey.set(priceKey(request), request);
    }
  }
  return [...byKey.values()];
}

function emitAll(): void {
  for (const emit of subscribers.keys()) emit();
}

function clearTimers(): void {
  if (pollTimer !== undefined) window.clearInterval(pollTimer);
  if (retryTimer !== undefined) window.clearTimeout(retryTimer);
  pollTimer = undefined;
  retryTimer = undefined;
}

function scheduleRetry(): void {
  if (retriesLeft <= 0 || retryTimer !== undefined) return;
  retriesLeft -= 1;
  retryTimer = window.setTimeout(() => {
    retryTimer = undefined;
    void fetchOnce();
  }, LOADING_RETRY_MS);
}

async function fetchOnce(): Promise<void> {
  if (!currentRequests.length) return;
  const my = ++fetchSeq;
  try {
    const res = await fetch(pricingUrl(currentApi), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ models: currentRequests }),
    });
    if (!res.ok) {
      // Includes 503 while the Usage extension is disabled or quarantined.
      if (my === fetchSeq) {
        cached = { ...cached, unavailable: !cached.meta };
        emitAll();
      }
      scheduleRetry();
      return;
    }
    const data = await res.json();
    if (my !== fetchSeq || !data?.prices) return;
    cached = { prices: data.prices, meta: data.meta ?? null, unavailable: false };
    emitAll();
    // A cold table means the extension answered before its first upstream
    // fetch landed; the numbers arrive seconds later.
    if (data.meta?.loading) scheduleRetry();
  } catch {
    if (my === fetchSeq && !cached.meta) {
      cached = { ...cached, unavailable: true };
      emitAll();
    }
    scheduleRetry();
  }
}

function reconcile(apiBase: string): void {
  const requests = unionRequests();
  const sig = requestSig(requests);
  if (apiBase === currentApi && sig === currentSig) return;
  currentApi = apiBase;
  currentSig = sig;
  currentRequests = requests;
  clearTimers();
  if (!requests.length) {
    fetchSeq += 1;
    return;
  }
  retriesLeft = LOADING_RETRY_LIMIT;
  void fetchOnce();
  pollTimer = window.setInterval(() => {
    retriesLeft = LOADING_RETRY_LIMIT;
    void fetchOnce();
  }, REFRESH_INTERVAL_MS);
}

/** Subscribes to the shared per-model price table. Fail-soft: pricing is
 * informational and never blocks anything. The table is an external store
 * — `cached` is replaced wholesale on every update, so a snapshot read is
 * stable between updates. */
export function usePricing(apiBase: string, requests: PricingRequest[]): Pricing {
  const sig = requestSig(requests);
  const subscribe = useCallback(
    (onStoreChange: Emit) => {
      subscribers.set(onStoreChange, requests);
      reconcile(apiBase);
      return () => {
        subscribers.delete(onStoreChange);
        reconcile(apiBase);
      };
    },
    // `requests` is fully determined by `sig`; depending on the array's
    // identity would re-subscribe on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [apiBase, sig],
  );
  return useSyncExternalStore(subscribe, getSnapshot);
}
