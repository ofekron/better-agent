import { useEffect, useState } from "react";
import { quotaStatusUrl, type QuotaStatus } from "../utils/quotaStatus";

// Same cadence as the usage-gauge extension module.
const REFRESH_INTERVAL_MS = 5 * 60 * 1000;

/** Polls the Usage extension's quota-status endpoint and returns a
 * per-kind map. Best-effort: any failure (extension missing, offline,
 * non-200) leaves the previous reading in place so pickers never break. */
export function useQuotaStatus(apiBase: string): QuotaStatus {
  const [status, setStatus] = useState<QuotaStatus>({});

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await fetch(quotaStatusUrl(apiBase));
        if (!res.ok) return;
        const data = await res.json();
        if (data?.providers && !cancelled) setStatus(data.providers);
      } catch {
        // Keep the previous reading; quota is advisory, never blocking.
      }
    };
    tick();
    const timer = window.setInterval(tick, REFRESH_INTERVAL_MS);
    const onVisible = () => {
      if (!document.hidden) tick();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [apiBase]);

  return status;
}
