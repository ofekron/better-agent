import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../../api";
import { trackedFetch } from "../../progress/store";
import type { Provider } from "../../types";
import type { HarnessProfile } from "../../types";
import { effortsForRunner } from "../modelPicker";
import type { HarnessFieldWrite } from "./types";
import { useProviderModelCatalog } from "../../hooks/useProviderModelCatalog";
import { useRuntimeProfiles } from "../../hooks/useRuntimeProfiles";
import { ModelCatalogStatus } from "../ModelCatalogStatus";

interface ProfileSummary {
  id: string;
  name: string;
}

interface Props {
  profile: HarnessProfile;
  /** All profiles (incl. Default), for the base-profile dropdown. */
  profiles: ProfileSummary[];
  disabled: boolean;
  onWrite: (write: HarnessFieldWrite) => void;
}

const BASE_FIELD = "base_profile_id";
const RUNTIME_PROFILE_FIELD = "default_runtime_profile_id";
const MODEL_FIELD = "default_model";
const EFFORT_FIELD = "default_reasoning_effort";
const PROVISIONING_PROMPT_FIELD = "provisioning_prompt";

/** Base pointer + runtime-profile/model/effort pins. These are scalar profile
 * fields, not deltas over Default, so they render here rather than through the
 * generic descriptor renderer. Every write routes through the same /fields
 * path. */
export function HarnessProfileMeta({ profile, profiles, disabled, onWrite }: Props) {
  const { t } = useTranslation();
  const [providers, setProviders] = useState<Provider[]>([]);
  const { snapshot, profiles: runtimeProfiles } = useRuntimeProfiles();

  const pinnedRuntimeProfileId = profile.default_runtime_profile_id ?? "";
  const pinnedModel = profile.default_model ?? "";
  const pinnedEffort = profile.default_reasoning_effort ?? "";
  const provisioningPrompt = profile.provisioning_prompt ?? "";
  const baseId = profile.base_profile_id ?? "";
  const pinnedRuntimeProfile =
    (snapshot?.runtime_profiles ?? []).find((item) => item.id === pinnedRuntimeProfileId)
    ?? null;
  const {
    catalog,
    networkState,
    refresh,
    refreshing,
    refreshError,
  } = useProviderModelCatalog(pinnedRuntimeProfile?.provider_id ?? "");
  const models = catalog?.models ?? [];
  const [provisioningPromptDraft, setProvisioningPromptDraft] = useState(provisioningPrompt);

  useEffect(() => {
    let cancelled = false;
    trackedFetch("harnessProfiles:providers", `${API}/api/providers`)
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then((data: { providers?: Provider[] }) => {
        if (!cancelled) setProviders(data.providers ?? []);
      })
      .catch(() => {
        if (!cancelled) setProviders([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    setProvisioningPromptDraft(provisioningPrompt);
  }, [provisioningPrompt]);

  // A profile cannot base on itself or on the synthesized Default; the backend
  // rejects a deeper cycle at save time and surfaces it as a patch error.
  const baseOptions = useMemo(
    () => profiles.filter((item) => item.id !== profile.id && item.id !== "default"),
    [profiles, profile.id],
  );

  const pinnedProvider = providers.find(
    (item) => item.id === pinnedRuntimeProfile?.provider_id,
  );
  // Keep the current pin visible even if the live catalog no longer lists it.
  const modelOptions = [...models];
  if (pinnedModel && !modelOptions.includes(pinnedModel)) {
    modelOptions.unshift(pinnedModel);
  }
  const effortOptions = pinnedProvider && pinnedRuntimeProfile
    ? effortsForRunner(pinnedProvider, pinnedRuntimeProfile.runner)
    : [];
  // A tombstoned pin stays visible (badged) so the stored value is truthful;
  // only live profiles are offered for new picks.
  const pinnedTombstone = pinnedRuntimeProfile?.deleted_at ? pinnedRuntimeProfile : null;

  const write = (field: string, value: string) => onWrite({ path: ["profile_meta", field], value });

  return (
    <article className="harness-extension-block harness-profile-meta">
      <div className="harness-extension-block-title">{t("harnessProfile.group.profile_meta")}</div>

      <div className="harness-item-row">
        <span className="harness-item-label">{t("harnessProfile.baseProfileLabel")}</span>
        <select
          className="harness-setting-input"
          value={baseId}
          disabled={disabled}
          onChange={(e) => write(BASE_FIELD, e.target.value)}
        >
          <option value="">{t("harnessProfile.baseProfileNone")}</option>
          {baseOptions.map((item) => (
            <option key={item.id} value={item.id}>{item.name}</option>
          ))}
        </select>
        <p className="harness-item-hint">{t("harnessProfile.baseProfileHint")}</p>
      </div>

      <div className="harness-item-row">
        <span className="harness-item-label">{t("harnessProfile.defaultRuntimeProfileLabel")}</span>
        <select
          className="harness-setting-input"
          value={pinnedRuntimeProfileId}
          disabled={disabled}
          onChange={(e) => write(RUNTIME_PROFILE_FIELD, e.target.value)}
        >
          <option value="">{t("harnessProfile.pinNone")}</option>
          {pinnedTombstone ? (
            <option value={pinnedTombstone.id} disabled>
              {pinnedTombstone.name} — {t("runtimeProfile.deletedBadge")}
            </option>
          ) : null}
          {runtimeProfiles.map((item) => (
            <option key={item.id} value={item.id}>{item.name}</option>
          ))}
        </select>
        <p className="harness-item-hint">{t("harnessProfile.pinHint")}</p>
      </div>

      <div className="harness-item-row">
        <span className="harness-item-label">{t("harnessProfile.defaultModelLabel")}</span>
        <select
          className="harness-setting-input"
          value={pinnedModel}
          disabled={disabled || !pinnedRuntimeProfileId}
          onChange={(e) => write(MODEL_FIELD, e.target.value)}
        >
          <option value="">{t("harnessProfile.pinNone")}</option>
          {modelOptions.map((model) => (
            <option key={model} value={model} disabled={!models.includes(model)}>
              {model}
              {!models.includes(model) ? ` — ${t("model.notSelectable")}` : ""}
            </option>
          ))}
        </select>
        <ModelCatalogStatus
          catalog={catalog}
          networkState={networkState}
          onRefresh={refresh}
          refreshing={refreshing}
          refreshError={refreshError}
        />
      </div>

      <div className="harness-item-row">
        <span className="harness-item-label">{t("harnessProfile.defaultReasoningEffortLabel")}</span>
        <select
          className="harness-setting-input"
          value={pinnedEffort}
          disabled={disabled || !pinnedRuntimeProfileId}
          onChange={(e) => write(EFFORT_FIELD, e.target.value)}
        >
          <option value="">{t("harnessProfile.pinNone")}</option>
          {effortOptions.map((effort) => (
            <option key={effort} value={effort}>{effort}</option>
          ))}
        </select>
      </div>

      <div className="harness-item-row">
        <span className="harness-item-label">{t("harnessProfile.provisioningPromptLabel")}</span>
        <textarea
          key={provisioningPrompt}
          className="harness-setting-input"
          value={provisioningPromptDraft}
          disabled={disabled}
          rows={5}
          placeholder={t("harnessProfile.provisioningPromptPlaceholder")}
          onChange={(e) => setProvisioningPromptDraft(e.target.value)}
          onBlur={() => {
            if (provisioningPromptDraft !== provisioningPrompt) {
              write(PROVISIONING_PROMPT_FIELD, provisioningPromptDraft);
            }
          }}
        />
        <p className="harness-item-hint">{t("harnessProfile.provisioningPromptHint")}</p>
      </div>
    </article>
  );
}
