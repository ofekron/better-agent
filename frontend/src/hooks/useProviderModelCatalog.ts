import { useCallback, useEffect, useRef, useState } from "react";
import { API } from "../api";
import { trackPromise, useOpProgress } from "../progress/store";
import type {
  ModelCatalog,
  ModelCatalogStatus,
  ModelRuntimeCatalogProfile,
  RetiredModel,
} from "../types";
import { useProviderCatalogRevision } from "./useModelsCatalogChanged";

const STATUSES = new Set<ModelCatalogStatus>([
  "pending",
  "refreshing",
  "current",
  "stale",
  "error",
  "unsupported",
  "unavailable",
]);

function isRetiredModel(value: unknown): value is RetiredModel {
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record.id === "string"
    && typeof record.first_absent_at === "number"
    && Number.isFinite(record.first_absent_at)
  );
}

function isRuntimeProfile(value: unknown): value is ModelRuntimeCatalogProfile {
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record.runner === "string"
    && typeof record.model === "string"
    && Array.isArray(record.reasoning_efforts)
    && record.reasoning_efforts.every((effort) => typeof effort === "string")
  );
}

function parseModelCatalog(value: unknown): ModelCatalog {
  if (!value || typeof value !== "object") {
    throw new Error("invalid model catalog response");
  }
  const record = value as Record<string, unknown>;
  const status = record.status;
  if (
    typeof record.provider_id !== "string"
    || typeof record.provider_generation !== "string"
    || typeof record.authoritative !== "boolean"
    || typeof status !== "string"
    || !STATUSES.has(status as ModelCatalogStatus)
    || !Array.isArray(record.models)
    || !record.models.every((model) => typeof model === "string")
    || typeof record.models_current !== "boolean"
    || !Array.isArray(record.retired)
    || !record.retired.every((model) => typeof model === "string")
    || !Array.isArray(record.retired_models)
    || !record.retired_models.every(isRetiredModel)
    || typeof record.last_refreshed_at !== "number"
    || typeof record.reason !== "string"
    || typeof record.authority_fingerprint !== "string"
  ) {
    throw new Error("invalid model catalog response");
  }
  const lastKnownGood = record.last_known_good;
  if (
    lastKnownGood !== null
    && (
      !lastKnownGood
      || typeof lastKnownGood !== "object"
      || !Array.isArray((lastKnownGood as Record<string, unknown>).models)
      || !(lastKnownGood as { models: unknown[] }).models.every(
        (model) => typeof model === "string",
      )
      || typeof (lastKnownGood as Record<string, unknown>).last_refreshed_at !== "number"
    )
  ) {
    throw new Error("invalid model catalog response");
  }
  const runtimeProfiles = record.runtime_profiles ?? [];
  if (!Array.isArray(runtimeProfiles) || !runtimeProfiles.every(isRuntimeProfile)) {
    throw new Error("invalid model catalog response");
  }
  if (
    (status === "current" && record.models_current !== true)
    || (
      (
        status === "pending"
        || status === "stale"
        || status === "error"
        || status === "unsupported"
        || status === "unavailable"
      )
      && record.models_current !== false
    )
  ) {
    throw new Error("inconsistent model catalog status");
  }
  return {
    provider_id: record.provider_id,
    provider_generation: record.provider_generation,
    authoritative: record.authoritative,
    status: status as ModelCatalogStatus,
    models: record.models,
    models_current: record.models_current,
    retired: record.retired,
    retired_models: record.retired_models,
    last_refreshed_at: record.last_refreshed_at,
    reason: record.reason,
    authority_fingerprint: record.authority_fingerprint,
    last_known_good: lastKnownGood as ModelCatalog["last_known_good"],
    runtime_profiles: runtimeProfiles,
  };
}

async function fetchCatalog(providerId: string): Promise<ModelCatalog> {
  const response = await fetch(
    `${API}/api/providers/${encodeURIComponent(providerId)}/models`,
  );
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const catalog = parseModelCatalog(await response.json());
  if (
    catalog.provider_id !== providerId
    || !catalog.provider_generation
  ) {
    throw new Error("model catalog authority mismatch");
  }
  return catalog;
}

export type CatalogNetworkState = "idle" | "loading" | "online" | "offline";

export function useProviderModelCatalog(providerId: string) {
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null);
  const [networkState, setNetworkState] = useState<CatalogNetworkState>("idle");
  const revision = useProviderCatalogRevision(providerId);
  const requestSequence = useRef(0);
  const loadOpId = providerId ? `modelCatalog:load:${providerId}` : "";
  const refreshOpId = providerId ? `modelCatalog:refresh:${providerId}` : "";
  const refreshProgress = useOpProgress(refreshOpId);

  useEffect(() => {
    const sequence = ++requestSequence.current;
    if (!providerId) return;
    const { promise } = trackPromise(loadOpId, () => fetchCatalog(providerId));
    promise.then(
      (next) => {
        if (requestSequence.current !== sequence) return;
        setCatalog(next);
        setNetworkState("online");
      },
      () => {
        if (requestSequence.current !== sequence) return;
        setNetworkState("offline");
      },
    );
  }, [loadOpId, providerId, revision]);

  const refresh = useCallback(async () => {
    if (!providerId) return;
    const { promise } = trackPromise(refreshOpId, async () => {
      const response = await fetch(
        `${API}/api/providers/${encodeURIComponent(providerId)}/models/refresh`,
        { method: "POST" },
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json() as Record<string, unknown>;
      if (
        payload.accepted !== true
        || payload.provider_id !== providerId
        || typeof payload.provider_generation !== "string"
      ) {
        throw new Error("invalid model catalog refresh acknowledgement");
      }
      const acknowledgedCatalog = parseModelCatalog(payload.catalog);
      if (
        acknowledgedCatalog.provider_id !== providerId
        || acknowledgedCatalog.provider_generation
          !== payload.provider_generation
      ) {
        throw new Error("model catalog refresh authority mismatch");
      }
      setNetworkState("online");
      window.dispatchEvent(
        new CustomEvent("model_catalog_refresh_acknowledged", {
          detail: {
            provider_id: providerId,
            catalog: acknowledgedCatalog,
          },
        }),
      );
    });
    return promise;
  }, [providerId, refreshOpId]);

  const effectiveCatalog = (
    providerId && catalog?.provider_id === providerId
  ) ? catalog : null;
  const effectiveNetworkState: CatalogNetworkState = !providerId
    ? "idle"
    : effectiveCatalog
      ? networkState
      : networkState === "offline"
        ? "offline"
        : "loading";

  return {
    catalog: effectiveCatalog,
    networkState: effectiveNetworkState,
    loading: effectiveNetworkState === "loading",
    refreshing: refreshProgress.inflight,
    refreshError: refreshProgress.error,
    refresh,
  };
}
