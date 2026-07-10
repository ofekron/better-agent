import { useEffect, useRef, useState } from "react";
import type { Provider } from "../types";
import { scopedSnapshotKey, sharedSnapshotPoller } from "../lib/sharedSnapshotPoller";
import { useExtensionAuthScope } from "../components/ExtensionSlots";
import { quotaStatusUrl, type QuotaStatus } from "../utils/quotaStatus";

// Same cadence as the usage-gauge extension module.
const REFRESH_INTERVAL_MS = 5 * 60 * 1000;

// Module-level singleton: ONE poller + ONE visibility listener shared by every
// mounted picker, so opening multiple pickers (or RuntimeProfilePicker, which
// mounts twice in team mode) does not multiply outbound quota-status requests.
// The backend further dedups upstream provider calls via its 60s cache.
//
// Quota is resolved per provider entry — the POST body carries the active
// entries {id, kind, mode, base_url, config_dir, name}, and the response is
// keyed by `providerQuotaKey` (provider id, else "<kind>::<config_dir>").
// `apiBase` and the provider-set signature are latched: a stray caller or an
// unchanged set never repoints or re-starts the poller.

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
    out.push({
      id: p.id,
      kind: p.kind,
      mode: p.mode,
      base_url: p.base_url || "",
      config_dir: p.config_dir || "",
      name: p.name,
    });
  }
  return out;
}

async function fetchQuota(api: string, entries: QuotaAccountEntry[]): Promise<QuotaStatus> {
    const res = await fetch(quotaStatusUrl(api), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ providers: entries }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    return data?.providers ?? {};
}

/** Subscribes to the shared per-provider quota-status poll. The first mounted
 * consumer (or a change in the provider-set signature) starts/repoints the
 * poller; the last unmounted consumer stops it. Fail-soft. */
export function useQuotaStatus(apiBase: string, providers: Provider[]): QuotaStatus {
  const authScopeKey = useExtensionAuthScope();
  const [status, setStatus] = useState<QuotaStatus>({});
  const providersRef = useRef(providers);
  providersRef.current = providers;
  const sig = distinctQuotaAccounts(providers)
    .map((p) => `${p.id}:${p.kind}:${p.mode}:${p.base_url}:${p.config_dir}`)
    .join("|");
  useEffect(() => {
    const entries = distinctQuotaAccounts(providersRef.current);
    const poller = sharedSnapshotPoller(scopedSnapshotKey(apiBase, authScopeKey, `quota:${sig}`), {
      load: () => fetchQuota(apiBase, entries),
      minIntervalMs: REFRESH_INTERVAL_MS,
      cadenceMs: REFRESH_INTERVAL_MS,
    });
    setStatus(poller.current() ?? {});
    return poller.subscribe(setStatus);
  }, [apiBase, authScopeKey, sig]);
  return status;
}
