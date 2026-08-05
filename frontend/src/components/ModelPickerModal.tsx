import { useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import type { Provider, ReasoningEffort, RuntimeProfile, Session } from "../types";
import { providerDisplayName } from "../utils/providerDisplayName";
import { optionLabelWithQuota, summarizeProvider } from "../utils/quotaStatus";
import { useQuotaStatus } from "../hooks/useQuotaStatus";
import { useProviderModelCatalog } from "../hooks/useProviderModelCatalog";
import { useRuntimeProfiles } from "../hooks/useRuntimeProfiles";
import { HarnessProfileSelector } from "./HarnessProfileSelector";
import { ModelCatalogStatus } from "./ModelCatalogStatus";
import {
  changedUpdates,
  effortsForRuntime,
  effortForProfile,
  makeDraft,
  modelForProfile,
  runnerLabelKey,
  sessionProfileView,
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
  const { snapshot, profiles } = useRuntimeProfiles();
  const sessionView = sessionProfileView(session, snapshot);
  const [draft, setDraft] = useState<SelectorDraft>(() => makeDraft(session, snapshot));
  const [error, setError] = useState<string | null>(null);
  const [harnessProfilesReady, setHarnessProfilesReady] = useState(false);

  // Re-anchor the initial draft once the snapshot lands (the hook starts
  // null; `makeDraft` on null leaves runtime_profile_id empty for sessions
  // whose profile resolves via the legacy fallback match).
  const [draftAnchored, setDraftAnchored] = useState(!!snapshot);
  if (snapshot && !draftAnchored) {
    setDraftAnchored(true);
    setDraft(makeDraft(session, snapshot));
  }

  const allProfiles = snapshot?.runtime_profiles ?? [];
  const draftProfile = allProfiles.find((p) => p.id === draft.runtime_profile_id) ?? null;
  const providerOf = (profile: RuntimeProfile | null) =>
    profile ? providers.find((p) => p.id === profile.provider_id) ?? null : null;
  const draftProvider = providerOf(draftProfile);
  // Legacy null-profile draft: model catalog and permissions come from the
  // session's raw stamped provider.
  const effectiveProviderId = draftProfile?.provider_id || session.provider_id || "";
  const effectiveProvider = draftProvider
    ?? providers.find((p) => p.id === effectiveProviderId)
    ?? null;
  const effectiveRunner = draftProfile?.runner ?? session.runner ?? "native";

  const {
    catalog,
    networkState,
    loading: loadingModels,
    refresh,
    refreshing,
    refreshError,
  } = useProviderModelCatalog(effectiveProviderId);
  const models = catalog?.models ?? [];
  const retired = catalog?.retired ?? [];
  const busy = disabled || saving;

  const changeDraftProfile = (profileId: string) => {
    const nextProfile = profiles.find((p) => p.id === profileId);
    if (!nextProfile) return;
    const nextProvider = providers.find((p) => p.id === nextProfile.provider_id);
    if (nextProvider?.suspended) return;
    const efforts = nextProvider
      ? effortsForRuntime(nextProvider, nextProfile.runner, "", [])
      : [];
    setDraft((current) => ({
      runtime_profile_id: profileId,
      model: modelForProfile(nextProfile, snapshot, []),
      reasoning_effort: effortForProfile(nextProfile, snapshot, efforts),
      permission: nextProvider?.default_permission || {},
      harness_profile_id: current.harness_profile_id,
    }));
  };

  const confirm = () => {
    if (!draft || busy || !harnessProfilesReady) return;
    if (!draft.model) {
      setError(t("sessionSelector.noModelForProfile", "No model is available for this runtime profile."));
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
  const draftKeepsSessionRuntime = draft.runtime_profile_id === (session.runtime_profile_id || sessionView.profile?.id || "");
  const sessionModelForRuntime = draftKeepsSessionRuntime ? session.model : "";
  for (const item of [draft.model, sessionModelForRuntime, ...models, ...retired]) {
    if (!item || seenModels.has(item)) continue;
    seenModels.add(item);
    modelOptions.push(item);
  }

  const draftQuota = summarizeProvider(quotaStatus, effectiveProvider ?? undefined);
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
      draftKeepsSessionRuntime
      && draft.model === session.model
      && knownModels.has(draft.model)
    )
  );
  const effortOptions = effectiveProvider
    ? effortsForRuntime(effectiveProvider, effectiveRunner, draft.model, runtimeProfiles)
    : [];

  // Session's own resolved profile when it is tombstoned — shown as a
  // disabled, badged option so the current selection stays truthful; the
  // backend rejects switching TO a deleted profile.
  const tombstonedSessionProfile = sessionView.deleted ? sessionView.profile : null;
  // Legacy session with no resolvable profile: keep a synthetic option
  // showing the raw stamped provider/runner values.
  const legacyOptionNeeded = !draft.runtime_profile_id;
  const legacyProvider = providers.find((p) => p.id === session.provider_id);
  const legacyLabel = [
    legacyProvider ? providerDisplayName(legacyProvider) : session.provider_id || "",
    session.runner
      ? t(runnerLabelKey(legacyProvider?.kind || "", session.runner))
      : "",
  ].filter(Boolean).join(" / ");

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
            <span>{t("runtimeProfile.label", "Runtime profile")}</span>
            <select
              value={draft.runtime_profile_id}
              disabled={busy}
              onChange={(e) => changeDraftProfile(e.target.value)}
            >
              {legacyOptionNeeded ? (
                <option value="" disabled>
                  {legacyLabel
                    ? `${legacyLabel} — ${t("runtimeProfile.legacyBadge", "no profile")}`
                    : t("runtimeProfile.select", "Select a runtime profile")}
                </option>
              ) : null}
              {tombstonedSessionProfile ? (
                <option value={tombstonedSessionProfile.id} disabled>
                  {tombstonedSessionProfile.name} — {t("runtimeProfile.deletedBadge", "Deleted")}
                </option>
              ) : null}
              {profiles.map((profile) => {
                const provider = providerOf(profile);
                const q = summarizeProvider(quotaStatus, provider ?? undefined);
                const suspended = !!provider?.suspended;
                return (
                  <option key={profile.id} value={profile.id} disabled={suspended}>
                    {optionLabelWithQuota(profile.name, q, t)}
                    {suspended ? ` - ${t("setup.suspended", "Suspended")}` : ""}
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
              onChange={(e) => {
                const model = e.target.value;
                const options = effectiveProvider
                  ? effortsForRuntime(effectiveProvider, effectiveRunner, model, runtimeProfiles)
                  : [];
                const reasoning_effort = options.includes(draft.reasoning_effort as ReasoningEffort)
                  ? draft.reasoning_effort
                  : draftProfile
                    ? effortForProfile(draftProfile, snapshot, options)
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
          {effortOptions.length ? (
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
                {effortOptions.map((effort) => (
                  <option key={effort} value={effort}>{t(`reasoningEffort.${effort}`, effort)}</option>
                ))}
              </select>
            </label>
          ) : null}
          {effectiveProvider?.permission_options
            ? Object.entries(effectiveProvider.permission_options).map(([axis, allowed]) => (
              <label className="session-model-picker-field" key={axis}>
                <span>{axis}</span>
                <select
                  value={draft.permission[axis] ?? effectiveProvider.default_permission?.[axis] ?? allowed[0] ?? ""}
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
            onReadyChange={setHarnessProfilesReady}
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
              || !harnessProfilesReady
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
