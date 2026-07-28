import { useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import type { Provider, ReasoningEffort, Session } from "../types";
import { providerDisplayName } from "../utils/providerDisplayName";
import { optionLabelWithQuota, summarizeProvider } from "../utils/quotaStatus";
import { useQuotaStatus } from "../hooks/useQuotaStatus";
import { useProviderModelCatalog } from "../hooks/useProviderModelCatalog";
import { HarnessProfileSelector } from "./HarnessProfileSelector";
import { ModelCatalogStatus } from "./ModelCatalogStatus";
import {
  changedUpdates,
  effortsForRuntime,
  makeDraft,
  modelForProvider,
  runnerForProvider,
  runnerLabelKey,
  type SelectorDraft,
  type SelectorUpdates,
  type ModelRuntimeProfile,
} from "./modelPicker";

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
  const [error, setError] = useState<string | null>(null);

  const modelProviderId = draft.provider_id || selectedProviderId;
  const {
    catalog,
    networkState,
    loading: loadingModels,
    refresh,
    refreshing,
    refreshError,
  } = useProviderModelCatalog(modelProviderId);
  const models = catalog?.models ?? [];
  const retired = catalog?.retired ?? [];
  const busy = disabled || saving;

  const changeDraftProvider = (providerId: string) => {
    const nextProvider = providers.find((p) => p.id === providerId && !p.suspended);
    if (!nextProvider) return;
    setDraft((current) => ({
      provider_id: providerId,
      model: modelForProvider(nextProvider, []),
      reasoning_effort: nextProvider.default_reasoning_effort || "",
      runner: runnerForProvider(nextProvider),
      permission: nextProvider.default_permission || {},
      harness_profile_id: current.harness_profile_id,
    }));
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

  const seenModels = new Set<string>();
  const modelOptions: string[] = [];
  const sessionModelForProvider = draft.provider_id === selectedProviderId ? session.model : "";
  for (const item of [draft.model, sessionModelForProvider, ...models, ...retired]) {
    if (!item || seenModels.has(item)) continue;
    seenModels.add(item);
    modelOptions.push(item);
  }

  const draftProvider = draft ? providers.find((p) => p.id === draft.provider_id) : null;
  const draftQuota = summarizeProvider(quotaStatus, draftProvider);
  const runtimeProfiles = (catalog?.runtime_profiles ?? []) as ModelRuntimeProfile[];
  const knownModels = new Set([...models, ...retired]);
  const catalogBlocksSelection = (
    catalog?.status === "pending"
    || catalog?.status === "unsupported"
    || catalog?.status === "unavailable"
  );
  const selectedModelValid = (
    models.includes(draft.model)
    || (
      draft.provider_id === selectedProviderId
      && draft.model === session.model
      && knownModels.has(draft.model)
    )
  );

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
                const q = summarizeProvider(quotaStatus, p);
                return (
                  <option key={p.id} value={p.id} disabled={p.suspended}>
                    {optionLabelWithQuota(providerDisplayName(p), q, t)}
                    {p.suspended ? ` - ${t("setup.suspended", "Suspended")}` : ""}
                  </option>
                );
              })}
            </select>
          </label>
          {draftProvider && draftProvider.runner_options.length > 1 ? (
            <label className="session-model-picker-field session-runtime-axis">
              <span>{t("newSession.runner")}</span>
              <select
                value={draft.runner}
                disabled={busy}
                onChange={(e) => {
                  const runner = e.target.value as Provider["runner"];
                  const options = effortsForRuntime(draftProvider, runner, draft.model, runtimeProfiles);
                  const reasoning_effort = options.includes(draft.reasoning_effort as ReasoningEffort)
                    ? draft.reasoning_effort
                    : options.includes(draftProvider.default_reasoning_effort as ReasoningEffort)
                      ? draftProvider.default_reasoning_effort
                      : options[0] || "";
                  setDraft({ ...draft, runner, reasoning_effort });
                }}
              >
                {draftProvider.runner_options.map((runner) => (
                  <option key={runner} value={runner}>{t(runnerLabelKey(draftProvider.kind, runner))}</option>
                ))}
              </select>
            </label>
          ) : null}
          <label className="session-model-picker-field">
            <span>{t("newSession.model", "Model")}</span>
            <select
              value={draft.model}
              disabled={busy || loadingModels || !modelOptions.length}
              onChange={(e) => {
                const model = e.target.value;
                const options = draftProvider
                  ? effortsForRuntime(draftProvider, draft.runner, model, runtimeProfiles)
                  : [];
                const reasoning_effort = options.includes(draft.reasoning_effort as ReasoningEffort)
                  ? draft.reasoning_effort
                  : options.includes(draftProvider?.default_reasoning_effort as ReasoningEffort)
                    ? draftProvider?.default_reasoning_effort || ""
                    : options[0] || "";
                setDraft({ ...draft, model, reasoning_effort });
              }}
            >
              {modelOptions.length ? (
                <>
                  {!draft.model ? (
                    <option value="">{t("sessionSelector.selectModel", "Select a model")}</option>
                  ) : null}
                  {modelOptions.map((m) => (
                    <option key={m} value={m} disabled={!models.includes(m)}>
                      {optionLabelWithQuota(m, draftQuota, t)}
                      {!models.includes(m) ? ` — ${t("model.notSelectable")}` : ""}
                    </option>
                  ))}
                </>
              ) : (
                <option value="">{t("sessionSelector.noModelsAvailable", "No models available")}</option>
              )}
            </select>
          </label>
          <ModelCatalogStatus
            catalog={catalog}
            networkState={networkState}
            onRefresh={refresh}
            refreshing={refreshing}
            refreshError={refreshError}
          />
          {draftProvider && effortsForRuntime(draftProvider, draft.runner, draft.model, runtimeProfiles).length ? (
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
                {effortsForRuntime(draftProvider, draft.runner, draft.model, runtimeProfiles).map((effort) => (
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
          <HarnessProfileSelector
            value={draft.harness_profile_id}
            disabled={busy}
            onChange={(harness_profile_id) =>
              setDraft({ ...draft, harness_profile_id })
            }
          />
          {error ? <div className="session-model-picker-error">{error}</div> : null}
        </div>
        <div className="modal-actions session-model-picker-actions">
          <button type="button" className="btn-secondary" onClick={onClose} disabled={busy}>
            {t("newSession.cancel", "Cancel")}
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={confirm}
            disabled={
              busy
              || !draft.model
              || catalogBlocksSelection
              || !selectedModelValid
            }
          >
            {saving ? t("sessionSelector.applying", "Applying...") : t("common.ok", "OK")}
          </button>
        </div>
      </div>
    </div>
  );
}
