import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import type { Permission, Provider, ReasoningEffort, Session } from "../types";
import { trackedFetch, useOpProgress } from "../progress/store";
import { cacheProviderModels, readProviderCache } from "../utils/providerCache";

interface Props {
  session: Session;
  providers: Provider[];
  disabled?: boolean;
  clientId?: string;
  onChange: (updates: SelectorUpdates) => void;
  onSaved?: () => void;
}

interface ModelCatalog {
  models?: string[];
}

interface ModelCatalogState {
  providerId: string;
  models: string[];
  error?: string;
}

type SelectorUpdates = Partial<Pick<Session, "provider_id" | "model" | "reasoning_effort" | "permission">>;

interface SelectorDraft {
  provider_id: string;
  model: string;
  reasoning_effort: ReasoningEffort | "";
  permission: Permission;
}

const providerModelsOp = (providerId: string) => `sessionSelector:models:${providerId}`;
const saveOp = (sessionId: string) => `sessionSelector:save:${sessionId}`;

function makeDraft(session: Session, providerId: string, providers: Provider[]): SelectorDraft {
  const provider = providers.find((p) => p.id === providerId);
  return {
    provider_id: providerId,
    model: session.model || provider?.last_model || provider?.default_model || "",
    reasoning_effort: session.reasoning_effort ?? "",
    permission: session.permission ?? {},
  };
}

function modelForProvider(provider: Provider, models: string[]): string {
  return provider.last_model || provider.default_model || models[0] || "";
}

function changedUpdates(session: Session, draft: SelectorDraft): SelectorUpdates {
  const updates: SelectorUpdates = {};
  if (draft.provider_id !== (session.provider_id || "")) updates.provider_id = draft.provider_id;
  if (draft.model !== (session.model || "")) updates.model = draft.model;
  if (draft.reasoning_effort !== (session.reasoning_effort || "")) updates.reasoning_effort = draft.reasoning_effort;
  if (JSON.stringify(draft.permission) !== JSON.stringify(session.permission ?? {})) updates.permission = draft.permission;
  return updates;
}

export function SessionSelectorControls({
  session,
  providers,
  disabled = false,
  clientId,
  onChange,
  onSaved,
}: Props) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<SelectorDraft | null>(null);
  const [modelsResult, setModelsResult] = useState<ModelCatalogState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const selectedProviderId = session.provider_id || providers.find((p) => !p.suspended)?.id || "";
  const modelProviderId = draft?.provider_id || selectedProviderId;
  const cachedModels = modelProviderId ? readProviderCache()?.modelsByProvider[modelProviderId] ?? [] : [];
  const models = modelsResult?.providerId === modelProviderId ? modelsResult.models : cachedModels;
  const modelLoadError = modelsResult?.providerId === modelProviderId ? modelsResult.error ?? null : null;
  const saving = useOpProgress(saveOp(session.id)).inflight;
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

  const save = async (updates: SelectorUpdates, optimistic: SelectorUpdates) => {
    setError(null);
    const prev = {
      provider_id: session.provider_id,
      model: session.model,
      reasoning_effort: session.reasoning_effort,
      permission: session.permission,
    };
    onChange(optimistic);
    try {
      const r = await trackedFetch(
        saveOp(session.id),
        `${API}/api/sessions/${encodeURIComponent(session.id)}/selectors`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...updates, client_id: clientId }),
        },
      );
      const body = await r.json().catch(() => null) as { updates?: Partial<Session> } | null;
      if (body?.updates) onChange(body.updates);
      setDraft(null);
      onSaved?.();
    } catch (e) {
      onChange(prev);
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const openPicker = () => {
    if (busy) return;
    setError(null);
    setDraft(makeDraft(session, selectedProviderId, providers));
  };

  const changeDraftProvider = (providerId: string) => {
    const nextProvider = providers.find((p) => p.id === providerId && !p.suspended);
    if (!nextProvider) return;
    const cachedModels = readProviderCache()?.modelsByProvider[providerId] ?? [];
    setDraft({
      provider_id: providerId,
      model: modelForProvider(nextProvider, cachedModels),
      reasoning_effort: nextProvider.default_reasoning_effort || "",
      permission: nextProvider.default_permission || {},
    });
  };

  const confirmPicker = () => {
    if (!draft || busy) return;
    if (!draft.model) {
      setError(t("sessionSelector.noModelForProvider", "No model is available for this provider."));
      return;
    }
    const updates = changedUpdates(session, draft);
    if (!Object.keys(updates).length) {
      setDraft(null);
      return;
    }
    void save(updates, updates);
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

  const selectedProvider = providers.find((p) => p.id === selectedProviderId);
  const draftProvider = draft ? providers.find((p) => p.id === draft.provider_id) : null;
  const selectorSummary = [selectedProvider?.name, session.model].filter(Boolean).join(" / ");

  if (!providers.length) return null;

  return (
    <div
      className="session-selector-controls"
      title={t(
        "chat.sessionSelectorsHint",
        "Change this session's provider/model. The next prompt continues in a fresh provider subprocess if needed.",
      )}
    >
      <button
        type="button"
        className="session-selector-picker-button"
        onClick={openPicker}
        disabled={busy}
        aria-label={t("sessionSelector.openPicker", "Change session model")}
      >
        <span>{selectorSummary || t("sessionSelector.openPicker", "Change session model")}</span>
      </button>
      {saving || loadingModels ? <span className="session-selector-status">...</span> : null}
      {(error || modelLoadError) && !draft ? <span className="session-selector-error" title={error || modelLoadError || ""}>!</span> : null}
      {draft ? (
        <div className="modal-overlay session-model-picker-overlay" onClick={() => !busy && setDraft(null)}>
          <div
            className="modal-content session-model-picker-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="session-model-picker-title"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-header">
              <h2 id="session-model-picker-title">{t("sessionSelector.title", "Session model")}</h2>
              <button
                type="button"
                className="modal-close"
                onClick={() => setDraft(null)}
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
                  {providers.map((p) => (
                    <option key={p.id} value={p.id} disabled={p.suspended}>
                      {p.name}{p.suspended ? ` - ${t("setup.suspended", "Suspended")}` : ""}
                    </option>
                  ))}
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
                      {modelOptions.map((m) => <option key={m} value={m}>{m}</option>)}
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
              <button type="button" className="btn-secondary" onClick={() => setDraft(null)} disabled={busy}>
                {t("newSession.cancel", "Cancel")}
              </button>
              <button type="button" className="btn-primary" onClick={confirmPicker} disabled={busy || !draft.model}>
                {saving ? t("sessionSelector.applying", "Applying...") : t("sessionSelector.apply", "Apply")}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
