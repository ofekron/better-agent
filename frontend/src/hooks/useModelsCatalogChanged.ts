import { useEffect } from "react";

// Subscribe to the cross-tab `models_catalog_changed` window event the
// App dispatches when the WS routes a per-provider catalog delta (see
// useWebSocket). Re-subscribes when `cb` identity changes — pass a
// stable (useCallback) reference to subscribe exactly once for the
// lifetime.
//
// Payload (on event.detail):
//   { provider_id, newly_added, became_active, went_retired, truly_removed }
// Subscribers typically refetch `/api/models`; the four disjoint
// transition sets exist so callers can also render toasts/badges
// per transition without dedup.
export function useModelsCatalogChanged(cb: (detail?: unknown) => void): void {
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      cb(detail);
    };
    window.addEventListener("models_catalog_changed", handler);
    return () =>
      window.removeEventListener("models_catalog_changed", handler);
  }, [cb]);
}
