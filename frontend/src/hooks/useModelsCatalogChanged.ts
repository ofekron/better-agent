import { useCallback, useEffect, useState } from "react";
import { eventBus, type BusEventMap } from "src/lib/eventBus";

type ModelsCatalogChanged = BusEventMap["models_catalog_changed"];

export function useModelsCatalogChanged(
  cb: (detail: ModelsCatalogChanged) => void,
): void {
  useEffect(() => {
    return eventBus.subscribe("models_catalog_changed", cb);
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
