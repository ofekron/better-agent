import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { parseModelCatalog } from "../hooks/useProviderModelCatalog";
import { eventBus } from "src/lib/eventBus";
import type { ModelCatalog } from "../types";
import Icon from "./Icon";

type Activity = {
  catalog: ModelCatalog;
  requestPending: boolean;
  acknowledgedSignature: string;
};

function signature(catalog: ModelCatalog): string {
  return [
    catalog.provider_generation,
    catalog.status,
    catalog.authority_fingerprint,
    catalog.last_refreshed_at,
  ].join(":");
}

async function fetchCatalogSnapshot(): Promise<ModelCatalog[]> {
  const response = await fetch(`${API}/api/model-catalogs`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const payload = await response.json() as Record<string, unknown>;
  if (!Array.isArray(payload.catalogs)) {
    throw new Error("invalid model catalog snapshot");
  }
  return payload.catalogs.map(parseModelCatalog);
}

function reconcileActivities(
  current: Record<string, Activity>,
  catalogs: ModelCatalog[],
): Record<string, Activity> {
  const next: Record<string, Activity> = {};
  for (const catalog of catalogs) {
    const existing = current[catalog.provider_id];
    if (catalog.status === "refreshing") {
      next[catalog.provider_id] = {
        catalog,
        requestPending: false,
        acknowledgedSignature: "",
      };
    } else if (
      existing?.requestPending
      && existing.acknowledgedSignature === signature(catalog)
    ) {
      next[catalog.provider_id] = existing;
    } else if (existing && catalog.status !== "current") {
      next[catalog.provider_id] = {
        catalog,
        requestPending: false,
        acknowledgedSignature: "",
      };
    }
  }
  return next;
}

export function ModelCatalogActivity() {
  const { t } = useTranslation();
  const [activities, setActivities] = useState<Record<string, Activity>>({});
  const requestSequence = useRef(0);

  const refreshSnapshot = useCallback(async () => {
    const sequence = ++requestSequence.current;
    try {
      const catalogs = await fetchCatalogSnapshot();
      if (requestSequence.current !== sequence) return;
      setActivities((current) => reconcileActivities(current, catalogs));
    } catch {
      return;
    }
  }, []);

  useEffect(() => {
    const sequence = ++requestSequence.current;
    void fetchCatalogSnapshot().then(
      (catalogs) => {
        if (requestSequence.current !== sequence) return;
        setActivities((current) => reconcileActivities(current, catalogs));
      },
      () => {},
    );
    const onChanged = () => {
      void refreshSnapshot();
    };
    const onAcknowledged = (event: Event) => {
      const detail = (event as CustomEvent).detail as
        | { provider_id?: string; catalog?: ModelCatalog }
        | undefined;
      const providerId = detail?.provider_id;
      const catalog = detail?.catalog;
      if (!providerId || !catalog) return;
      setActivities((current) => ({
        ...current,
        [providerId]: {
          catalog,
          requestPending: true,
          acknowledgedSignature: signature(catalog),
        },
      }));
    };
    const unsubscribeChanged = eventBus.subscribe(
      "models_catalog_changed",
      onChanged,
    );
    window.addEventListener(
      "model_catalog_refresh_acknowledged",
      onAcknowledged,
    );
    return () => {
      unsubscribeChanged();
      window.removeEventListener(
        "model_catalog_refresh_acknowledged",
        onAcknowledged,
      );
    };
  }, [refreshSnapshot]);

  const visible = Object.values(activities);
  if (!visible.length) return null;
  return (
    <aside className="model-catalog-activity" role="status" aria-live="polite">
      <header>
        <span>{t("model.catalogActivityTitle")}</span>
        <button
          type="button"
          onClick={() => setActivities({})}
          aria-label={t("model.catalogActivityDismiss")}
          title={t("model.catalogActivityDismiss")}
        >
          ×
        </button>
      </header>
      <ul>
        {visible.map(({ catalog, requestPending }) => (
          <li
            key={catalog.provider_id}
            className={`model-catalog-activity-${requestPending ? "pending" : catalog.status}`}
          >
            <Icon
              name={
                requestPending || catalog.status === "refreshing"
                  ? "refresh"
                  : "warning"
              }
              size={12}
            />
            <span>
              {requestPending
                ? t("model.catalogRefreshAccepted")
                : t(`model.catalogActivity.${catalog.status}`)}
            </span>
          </li>
        ))}
      </ul>
    </aside>
  );
}
