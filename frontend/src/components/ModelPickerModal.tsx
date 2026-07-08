import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import type { Provider, ReasoningEffort, Session } from "../types";
import { trackedFetch, useOpProgress } from "../progress/store";
import { cacheProviderModels, readProviderCache } from "../utils/providerCache";
import { optionLabelWithQuota, summarizeProvider } from "../utils/quotaStatus";
import { useQuotaStatus } from "../hooks/useQuotaStatus";
import { changedUpdates, makeDraft, modelForProvider, type SelectorDraft, type SelectorUpdates } from "./modelPicker";

interface ModelCatalog {
  models?: string[];
}

interface ModelCatalogState {
  providerId: string;
  models: string[];
  error?: string;
}

const providerModelsOp = (providerId: string) => `sessionSelector:models:${providerId}`;

interface Props {
  session: Session;
  providers: Provider[];
  /** Disabled flags from the caller (e.g. session offline). */
  disabled?: boolean;
  /** Caller-controlled save-in-progress flag; disables confirm + close while true. */
  saving?: boolean;
  title?: string;
  onConfirm: (updates: SelectorUpdates) => void;
  onClose: () => void;
}

export function ModelPickerModal({
  session,
  providers,
  disabled = false,
  saving = false,
  title,
  onConfirm,
  onClose,
}: Props) {
  const { t } = useTranslation();
  const quotaStatus = useQuotaStatus(API, providers);
  const selectedProviderId = session.provider_id || providers.find((p) => !p.suspended)?.id || "";
  const [draft, setDraft] = useState<SelectorDraft>(() => makeDraft(session, selectedProviderId, providers));
  const [modelsResult, setModelsResult] = useState<ModelCatalogState | null>(null);
  const [error, setError] = useState<string | null>(null);

  const modelProviderId = draft.provider_id || selectedProviderId;
  const cachedModels = modelProviderId ? readProviderCache()?.modelsByProvider[modelProviderId] ?? [] : [];
  const models = modelsResult?.providerId === modelProviderId ? modelsResult.models : cachedModels;
  const modelLoadError = modelsResult?.providerId === modelProviderId ? modelsResult.error ?? null : null;
  const loadingModels = useOpProgress(modelProviderId ? providerModelsOp(modelProviderId) : "").inflight;
  const busy = disabled || saving;

  useEffect(() => {
    let cancelled = false;
    if (!modelProviderId) return;
    trackedFetch(providerModelsOp(modelProviderId), `${API}/api/providers/${encodeURIComponent(modelProviderId)}/models`)
      .then((r) => r.json() as Promise<ModelCatalog>)
      .then((catalog) => {
        if (cancelled) return;
        const list = catalog.models || [];
        cacheProviderModels(modelProviderId, list);
        setModelsResult({ providerId: modelProviderId, models: list });
      })
      .catch((e) => {
        if (cancelled) return;
        setModelsResult({
          providerId: modelProviderId,
          models: readProviderCache()?.modelsByProvider[modelProviderId] ?? [],
          error: e instanceof Error ? e.message : String(e),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [modelProviderId]);

  const changeDraftProvider = (providerId: string) => {
    const nextProvider = providers.find((p) => p.id === providerId && !p.suspended);
    if (!nextProvider) return;
    const providerCachedModels = readProviderCache()?.modelsByProvider[providerId] ?? [];
    setDraft({
      provider_id: providerId,
      model: modelForProvider(nextProvider, providerCachedModels),
      reasoning_effort: nextProvider.default_reasoning_effort || "",
      permission: nextProvider.default_permission || {},
    });
  };

  const confirm = () => {
    if (!draft || busy) return;
    if (!draft.model) {
      setError(t("sessionSelector.noModelForProvider", "No model is available for this provider."));
      return;
    }
    const updates = changedUpdates(session, draft);
    if (!Object.keys(updates).length) {
      onClose();
      return;
    }
    onConfirm(updates);
  };

  const modelOptions = useMemo(() => {
    const seen = new Set<string>();
    const out: string[] = [];
    const sessionModelForProvider = draft?.provider_id === selectedProviderId ? session.model : "";
    for (const item of [draft?.model, sessionModelForProvider, ...models]) {
      if (!item || seen.has(item)) continue;
      seen.add(item);
      out.push(item);
    }
    return out;
  }, [draft?.model, draft?.provider_id, selectedProviderId, session.model, models]);

  const draftProvider = draft ? providers.find((p) => p.id === draft.provider_id) : null;
  const draftQuota = summarizeProvider(quotaStatus, draftProvider?.kind, draftProvider?.config_dir);

  return (
    <div className="modal-overlay session-model-picker-overlay" onClick={() => !busy && onClose()}>
      <div
        className="modal-content session-model-picker-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="session-model-picker-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2 id="session-model-picker-title">
            {title ?? t("sessionSelector.title", "Session model")}
          </h2>
          <button
            type="button"
            className="modal-close"
            onClick={onClose}
            disabled={busy}
            aria-label={t("common.close", "Close")}
          >
            &times;
          </button>
        </div>
        <div className="modal-body session-model-picker-body">
          <label className="session-model-picker-field">
            <span>{t("newSession.provider", "Provider")}</span>
            <select
              value={draft.provider_id}
              disabled={busy}
              onChange={(e) => changeDraftProvider(e.target.value)}
            >
              {providers.map((p) => {
                const q = summarizeProvider(quotaStatus, p.kind, p.config_dir);
                return (
                  <option key={p.id} value={p.id} disabled={p.suspended}>
                    {optionLabelWithQuota(p.name, q, t)}
                    {p.suspended ? ` - ${t("setup.suspended", "Suspended")}` : ""}
                  </option>
                );
              })}
            </select>
          </label>
          <label className="session-model-picker-field">
            <span>{t("newSession.model", "Model")}</span>
            <select
              value={draft.model}
              disabled={busy || loadingModels || !modelOptions.length}
              onChange={(e) => setDraft({ ...draft, model: e.target.value })}
            >
              {modelOptions.length ? (
                <>
                  {!draft.model ? (
                    <option value="">{t("sessionSelector.selectModel", "Select a model")}</option>
                  ) : null}
                  {modelOptions.map((m) => (
                    <option key={m} value={m}>{optionLabelWithQuota(m, draftQuota, t)}</option>
                  ))}
                </>
              ) : (
                <option value="">{t("sessionSelector.noModelsAvailable", "No models available")}</option>
              )}
            </select>
          </label>
          {draftProvider?.reasoning_effort_options?.length ? (
            <label className="session-model-picker-field">
              <span>{t("newSession.reasoningEffort", "Effort")}</span>
              <select
                value={draft.reasoning_effort}
                disabled={busy}
                onChange={(e) => setDraft({ ...draft, reasoning_effort: e.target.value as ReasoningEffort })}
              >
                {!draft.reasoning_effort ? (
                  <option value="">{t("reasoningEffort.none", "None")}</option>
                ) : null}
                {draftProvider.reasoning_effort_options.map((effort) => (
                  <option key={effort} value={effort}>{t(`reasoningEffort.${effort}`, effort)}</option>
                ))}
              </select>
            </label>
          ) : null}
          {draftProvider?.permission_options
            ? Object.entries(draftProvider.permission_options).map(([axis, allowed]) => (
              <label className="session-model-picker-field" key={axis}>
                <span>{axis}</span>
                <select
                  value={draft.permission[axis] ?? draftProvider.default_permission?.[axis] ?? allowed[0] ?? ""}
                  disabled={busy}
                  onChange={(e) => setDraft({ ...draft, permission: { ...draft.permission, [axis]: e.target.value } })}
                >
                  {allowed.map((value) => <option key={value} value={value}>{value}</option>)}
                </select>
              </label>
            ))
            : null}
          {error || modelLoadError ? <div className="session-model-picker-error">{error || modelLoadError}</div> : null}
        </div>
        <div className="modal-actions session-model-picker-actions">
          <button type="button" className="btn-secondary" onClick={onClose} disabled={busy}>
            {t("newSession.cancel", "Cancel")}
          </button>
          <button type="button" className="btn-primary" onClick={confirm} disabled={busy || !draft.model}>
            {saving ? t("sessionSelector.applying", "Applying...") : t("common.ok", "OK")}
          </button>
        </div>
      </div>
    </div>
  );
}
