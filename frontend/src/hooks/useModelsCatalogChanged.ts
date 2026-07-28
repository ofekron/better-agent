import { useCallback, useEffect, useState } from "react";

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

export function useProviderCatalogRevision(providerId: string): number {
  const [revision, setRevision] = useState(0);
  const invalidate = useCallback((detail?: unknown) => {
    const changedProviderId = (
      detail
      && typeof detail === "object"
      && "provider_id" in detail
    )
      ? String(detail.provider_id || "")
      : "";
    if (changedProviderId === providerId) {
      setRevision((current) => current + 1);
    }
  }, [providerId]);
  useModelsCatalogChanged(invalidate);
  return revision;
}
