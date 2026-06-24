import { useEffect } from "react";

// Subscribe to the cross-tab `provider_changed` window event the App
// dispatches when the WS routes a provider change (see useWebSocket).
// Re-subscribes when `cb` identity changes — pass a stable
// (useCallback) reference to subscribe exactly once for the lifetime.
export function useProviderChanged(cb: () => void): void {
  useEffect(() => {
    const handler = () => cb();
    window.addEventListener("provider_changed", handler);
    return () => window.removeEventListener("provider_changed", handler);
  }, [cb]);
}
