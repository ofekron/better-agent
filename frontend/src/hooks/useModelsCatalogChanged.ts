import { useCallback, useEffect, useState } from "react";
import { eventBus, type BusEventMap } from "src/lib/eventBus";
import { subscribeProviderFrames } from "./useProviderSurfaceSocket";

type ModelsCatalogChanged = BusEventMap["models_catalog_changed"];

/** Dual-subscribed like `useProviderChanged` (see its docstring): the
 * legacy `models_catalog_changed` broadcast (kept, unconditional) plus the
 * v2 `"providers"` feed's `model_catalog_changed` frame (added) — traced
 * source: `backend/providers_api.py`'s `broadcast_model_catalog_fact` and
 * `backend/adapters/provider_adapter.py`'s `_on_model_catalog_changed`
 * both fire from `FACT_MODEL_CATALOG_CHANGED`/the same diff-gated
 * `_publish_fact(FACT_MODEL_CATALOG_CHANGED, {"provider_id": ...})` call
 * sites in `providers_api.py`/`runtime_profiles_api.py` — same
 * `provider_id` payload shape either way, so `cb` gets called identically
 * regardless of which transport delivered the change. */
export function useModelsCatalogChanged(
  cb: (detail: ModelsCatalogChanged) => void,
): void {
  useEffect(() => {
    return eventBus.subscribe("models_catalog_changed", cb);
  }, [cb]);

  useEffect(() => {
    return subscribeProviderFrames((frame) => {
      if (frame.type === "model_catalog_changed") {
        cb({ provider_id: frame.provider_id });
      }
    });
  }, [cb]);
}

export function useProviderCatalogRevision(providerId: string): number {
  const [revision, setRevision] = useState(0);
  const invalidate = useCallback((detail: ModelsCatalogChanged) => {
    if (detail.provider_id === providerId) {
      setRevision((current) => current + 1);
    }
  }, [providerId]);
  useModelsCatalogChanged(invalidate);
  return revision;
}
