import { useTranslation } from "react-i18next";
import type { ModelCatalog } from "../types";
import type { CatalogNetworkState } from "../hooks/useProviderModelCatalog";

export function ModelCatalogStatus({
  catalog,
  networkState,
  onRefresh,
  refreshing = false,
  refreshError = null,
}: {
  catalog: ModelCatalog | null;
  networkState: CatalogNetworkState;
  onRefresh?: () => Promise<void> | undefined;
  refreshing?: boolean;
  refreshError?: string | null;
}) {
  const { t } = useTranslation();
  if (networkState === "offline") {
    return (
      <div className="model-catalog-status model-catalog-status-offline" role="status" aria-live="polite">
        {catalog
          ? t("model.catalogOfflineLkg")
          : t("model.catalogOffline")}
      </div>
    );
  }
  if (!catalog) return null;
  const refreshButton = (
    catalog.authoritative
    && catalog.status !== "unsupported"
    && catalog.status !== "unavailable"
    && onRefresh
  ) ? (
    <button
      type="button"
      onClick={() => void onRefresh()?.catch(() => {})}
      disabled={refreshing}
      aria-busy={refreshing}
    >
      {refreshing ? t("progress.refreshing") : t("files.refreshButton")}
    </button>
  ) : null;
  if (catalog.status === "current" && !refreshError) {
    return refreshButton ? (
      <div className="model-catalog-status model-catalog-status-current">
        {refreshButton}
      </div>
    ) : null;
  }
  if (catalog.status === "current") {
    return (
      <div
        className="model-catalog-status model-catalog-status-error"
        role="status"
        aria-live="polite"
      >
        <span>{t("model.catalogError")}</span>
        {refreshButton}
      </div>
    );
  }
  const key = {
    pending: "model.catalogPending",
    refreshing: "model.catalogRefreshing",
    stale: "model.catalogStale",
    error: "model.catalogError",
    unsupported: "model.catalogUnsupported",
    unavailable: "model.catalogUnavailable",
  } satisfies Record<typeof catalog.status, string>;
  return (
    <div
      className={`model-catalog-status model-catalog-status-${catalog.status}`}
      role="status"
      aria-live="polite"
      data-reason={catalog.reason}
    >
      <span>{refreshError ? t("model.catalogError") : t(key[catalog.status])}</span>
      {refreshButton}
    </div>
  );
}
