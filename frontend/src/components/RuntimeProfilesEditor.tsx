import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { Provider, ReasoningEffort, RuntimeProfile } from "../types";
import {
  activateRuntimeProfile,
  createRuntimeProfile,
  deleteRuntimeProfile,
  patchRuntimeProfile,
} from "../api";
import { useRuntimeProfiles } from "../hooks/useRuntimeProfiles";
import { useProviderModelCatalog } from "../hooks/useProviderModelCatalog";
import { EffortDefaultField, ModelDefaultField } from "./RuntimeProfileFields";
import { providerNickname } from "../utils/providerDisplayName";
import { effortsForRunner, runnerLabelKey } from "./modelPicker";

// ---------------------------------------------------------------------------
// Runtime-profile management: rail of profiles (tombstones greyed), edit of
// name/default model/default effort, Activate (the ONLY default-selection
// surface), soft-delete. Backend-owned state via useRuntimeProfiles
// (REST snapshot + runtime_profiles_changed frames).
// ---------------------------------------------------------------------------

type PendingAction = "save" | "activate" | "delete" | "restore" | null;

export function RuntimeProfilesEditor({
  providers,
  onCreateProfile,
}: {
  providers: Provider[];
  onCreateProfile: () => void;
}) {
  const { t } = useTranslation();
  const { snapshot, profiles, defaultProfileId, loading, error: loadError, refresh } =
    useRuntimeProfiles();
  const allProfiles = useMemo(
    () => snapshot?.runtime_profiles ?? [],
    [snapshot],
  );
  const tombstones = useMemo(
    () => allProfiles.filter((p) => !!p.deleted_at),
    [allProfiles],
  );

  const [selectedId, setSelectedId] = useState("");
  const selected: RuntimeProfile | null =
    allProfiles.find((p) => p.id === selectedId) ?? null;

  // Follow the backend default until the user picks a profile; follow a
  // vanished selection back to the default rather than showing a dead pane.
  useEffect(() => {
    if (!snapshot) return;
    if (selectedId && allProfiles.some((p) => p.id === selectedId)) return;
    setSelectedId(defaultProfileId ?? allProfiles[0]?.id ?? "");
  }, [snapshot, selectedId, allProfiles, defaultProfileId]);

  const provider = providers.find((p) => p.id === selected?.provider_id) ?? null;
  const deletedProvider =
    snapshot?.deleted_providers.find((p) => p.id === selected?.provider_id) ?? null;
  const isDeleted = !!selected?.deleted_at;
  const isDefault = !!selected && selected.id === defaultProfileId;

  // Draft of the editable fields, reseeded whenever the authoritative record
  // moves (selection change or backend update).
  const seedKey = selected ? `${selected.id}:${selected.updated_at}` : "";
  const [draft, setDraft] = useState({ name: "", model: "", effort: "" as ReasoningEffort | "" });
  useEffect(() => {
    if (!selected) return;
    setDraft({
      name: selected.name,
      model: selected.default_model,
      effort: selected.default_reasoning_effort,
    });
    // Reseed only when the record itself moved — seedKey encodes id+updated_at.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seedKey]);
  const dirty =
    !!selected
    && (draft.name !== selected.name
      || draft.model !== selected.default_model
      || draft.effort !== selected.default_reasoning_effort);

  const [pending, setPending] = useState<PendingAction>(null);
  const [actionError, setActionError] = useState("");

  const runAction = async (action: Exclude<PendingAction, null>, fn: () => Promise<unknown>) => {
    setPending(action);
    setActionError("");
    try {
      await fn();
      refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setPending(null);
    }
  };

  const effortOptions = provider && selected ? effortsForRunner(provider, selected.runner) : [];
  const catalogState = useProviderModelCatalog(!isDeleted && provider ? provider.id : "");

  const providerLabel = (p: RuntimeProfile): string => {
    const live = providers.find((item) => item.id === p.provider_id);
    if (live) {
      const nick = providerNickname(live);
      return nick ? `${live.name} — ${nick}` : live.name;
    }
    const dead = snapshot?.deleted_providers.find((item) => item.id === p.provider_id);
    return dead ? dead.name : p.provider_id;
  };
  const runnerLabel = (p: RuntimeProfile): string => {
    const live = providers.find((item) => item.id === p.provider_id);
    const dead = snapshot?.deleted_providers.find((item) => item.id === p.provider_id);
    const kind = live?.kind ?? dead?.kind ?? "";
    return kind ? t(runnerLabelKey(kind, p.runner)) : p.runner;
  };

  if (loading && !snapshot) {
    return <div className="runtime-profiles-editor">{t("common.loading", "Loading…")}</div>;
  }

  const railItem = (p: RuntimeProfile) => (
    <button
      key={p.id}
      type="button"
      data-testid={`rpe-rail-${p.id}`}
      className={`harness-profile-rail-item rpe-rail-item${p.id === selectedId ? " is-selected" : ""}${p.deleted_at ? " is-deleted" : ""}`}
      onClick={() => {
        setSelectedId(p.id);
        setActionError("");
      }}
    >
      <span className="rpe-rail-name">{p.name}</span>
      {p.id === defaultProfileId && (
        <span className="provider-active-pill">{t("runtimeProfile.defaultBadge")}</span>
      )}
      {p.deleted_at && (
        <span className="rpe-deleted-badge">{t("runtimeProfile.deletedBadge")}</span>
      )}
    </button>
  );

  return (
    <div className="runtime-profiles-editor">
      <div className="harness-settings-layout">
        <nav className="harness-profile-rail" aria-label={t("settings.runtimeProfilesSection")}>
          {profiles.map(railItem)}
          {tombstones.length > 0 && <div className="rpe-rail-divider" />}
          {tombstones.map(railItem)}
          <div className="harness-settings-create-row">
            <button
              type="button"
              className="btn-secondary"
              data-testid="rpe-create-profile"
              onClick={onCreateProfile}
            >
              {t("runtimeProfile.createCta")}
            </button>
          </div>
        </nav>

        <section className="harness-settings-pane">
          {loadError && (
            <div className="harness-settings-editor-error" role="alert">{loadError}</div>
          )}
          {!selected && (
            <p className="settings-hint">{t("runtimeProfile.none")}</p>
          )}
          {selected && (
            <div className={`rpe-pane${isDeleted ? " is-deleted" : ""}`}>
              <header className="harness-settings-header">
                <div className="harness-settings-header-title">
                  {selected.name}
                  {isDefault && (
                    <span className="provider-active-pill">{t("runtimeProfile.defaultBadge")}</span>
                  )}
                  {isDeleted && (
                    <span className="rpe-deleted-badge">{t("runtimeProfile.deletedBadge")}</span>
                  )}
                </div>
                <p className="harness-settings-header-hint">
                  {providerLabel(selected)}
                  {" · "}
                  {runnerLabel(selected)}
                  {deletedProvider && (
                    <span className="rpe-deleted-badge">
                      {t("runtimeProfile.deletedProviderBadge")}
                    </span>
                  )}
                </p>
              </header>

              {actionError && (
                <div className="harness-settings-editor-error" role="alert">{actionError}</div>
              )}

              {!isDeleted && (
                <>
                  <div className="setup-field">
                    <label>{t("runtimeProfile.nameLabel")}</label>
                    <input
                      type="text"
                      data-testid="rpe-name-input"
                      value={draft.name}
                      onChange={(e) => setDraft((prev) => ({ ...prev, name: e.target.value }))}
                      spellCheck={false}
                    />
                  </div>
                  <ModelDefaultField
                    catalogState={catalogState}
                    model={draft.model}
                    onModelChange={(model) => setDraft((prev) => ({ ...prev, model }))}
                  />
                  <EffortDefaultField
                    options={effortOptions}
                    effort={draft.effort}
                    onEffortChange={(effort) => setDraft((prev) => ({ ...prev, effort }))}
                  />
                </>
              )}

              <div className="provider-edit-actions">
                {!isDeleted && (
                  <button
                    type="button"
                    className="setup-save-btn"
                    data-testid="rpe-save"
                    disabled={pending !== null || !dirty || !draft.name.trim()}
                    onClick={() => void runAction("save", () =>
                      patchRuntimeProfile(selected.id, {
                        name: draft.name.trim(),
                        default_model: draft.model.trim(),
                        default_reasoning_effort: draft.effort,
                      }),
                    )}
                  >
                    {pending === "save" ? t("setup.saving") : t("setup.saveChanges")}
                  </button>
                )}
                {!isDeleted && !isDefault && (
                  <button
                    type="button"
                    className="btn-secondary"
                    data-testid="rpe-activate"
                    disabled={pending !== null || provider?.suspended === true}
                    onClick={() => void runAction("activate", () =>
                      activateRuntimeProfile(selected.id),
                    )}
                  >
                    {pending === "activate" && <span className="retrying-spinner" aria-hidden="true" />}
                    {pending === "activate"
                      ? t("runtimeProfile.activating")
                      : t("runtimeProfile.activate")}
                  </button>
                )}
                {!isDeleted && !isDefault && (
                  <button
                    type="button"
                    className="btn-danger"
                    data-testid="rpe-delete"
                    disabled={pending !== null}
                    onClick={() => {
                      if (!confirm(t("runtimeProfile.deleteConfirm"))) return;
                      void runAction("delete", () => deleteRuntimeProfile(selected.id));
                    }}
                  >
                    {pending === "delete" ? t("runtimeProfile.deleting") : t("runtimeProfile.deleteProfile")}
                  </button>
                )}
                {!isDeleted && isDefault && (
                  <span className="setup-field-hint">{t("runtimeProfile.defaultCannotDelete")}</span>
                )}
                {isDeleted && provider && (
                  <button
                    type="button"
                    className="btn-secondary"
                    data-testid="rpe-restore"
                    disabled={pending !== null}
                    onClick={() => void runAction("restore", () =>
                      // Recreating the (provider, runner) pair resurrects the
                      // tombstone under its original id (backend semantics).
                      createRuntimeProfile({
                        provider_id: selected.provider_id,
                        runner: selected.runner,
                      }),
                    )}
                  >
                    {pending === "restore" ? t("setup.saving") : t("runtimeProfile.restore")}
                  </button>
                )}
                {isDeleted && (
                  <span className="setup-field-hint">{t("runtimeProfile.tombstoneHint")}</span>
                )}
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
