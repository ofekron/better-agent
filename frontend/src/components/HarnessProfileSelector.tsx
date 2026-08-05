import { useEffect, useLayoutEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { eventBus } from "../lib/eventBus";
import { DEFAULT_HARNESS_PROFILE_ID } from "../lib/harnessProfile";
import { trackedFetch } from "../progress/store";
import type { HarnessProfile } from "../types";

interface Props {
  value?: string;
  disabled?: boolean;
  className?: string;
  onChange: (profileId: string) => void;
  onReadyChange?: (ready: boolean) => void;
}

const loadOp = "harnessProfiles:list";

export function HarnessProfileSelector({
  value = DEFAULT_HARNESS_PROFILE_ID,
  disabled = false,
  className = "session-model-picker-field",
  onChange,
  onReadyChange,
}: Props) {
  const { t } = useTranslation();
  const [profiles, setProfiles] = useState<HarnessProfile[] | null>(null);
  const [error, setError] = useState("");

  useLayoutEffect(() => {
    return () => onReadyChange?.(false);
  }, [onReadyChange]);

  useEffect(() => {
    let cancelled = false;
    let generation = 0;
    const load = () => {
      const requestGeneration = ++generation;
      onReadyChange?.(false);
      return trackedFetch(loadOp, `${API}/api/harness-profiles`)
        .then((response) => response.json() as Promise<{ profiles?: HarnessProfile[] }>)
        .then((body) => {
          if (cancelled || requestGeneration !== generation) return;
          setProfiles(body.profiles ?? []);
          setError("");
          onReadyChange?.(true);
        })
        .catch((err) => {
          if (cancelled || requestGeneration !== generation) return;
          setError(err instanceof Error ? err.message : String(err));
          onReadyChange?.(true);
        });
    };
    void load();
    const unsubscribe = eventBus.subscribe("harness_profiles_changed", () => {
      void load();
    });
    return () => {
      cancelled = true;
      generation += 1;
      unsubscribe();
    };
  }, [onReadyChange]);

  const requestedValue = value || DEFAULT_HARNESS_PROFILE_ID;
  const loadedProfiles = profiles ?? [];
  const profilesLoaded = profiles !== null;
  const selectedProfileAvailable = loadedProfiles.some((profile) => profile.id === requestedValue);
  const missingSavedProfile =
    requestedValue !== DEFAULT_HARNESS_PROFILE_ID && !selectedProfileAvailable;
  const effectiveValue = profilesLoaded && missingSavedProfile
    ? DEFAULT_HARNESS_PROFILE_ID
    : requestedValue;

  useLayoutEffect(() => {
    if (profilesLoaded && missingSavedProfile) onChange(DEFAULT_HARNESS_PROFILE_ID);
  }, [missingSavedProfile, onChange, profilesLoaded]);

  return (
    <div className={className}>
      <span>{t("harnessProfile.label", "Harness profile")}</span>
      <div className="harness-profile-selector-row">
        <select
          aria-label={t("harnessProfile.label", "Harness profile")}
          value={effectiveValue}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
        >
          {!profilesLoaded && missingSavedProfile ? (
            <option value={requestedValue}>{requestedValue}</option>
          ) : null}
          {loadedProfiles.length === 0 ? (
            <option value={DEFAULT_HARNESS_PROFILE_ID}>{t("harnessProfile.defaultOptionLabel")}</option>
          ) : null}
          {loadedProfiles.map((profile) => (
            <option key={profile.id} value={profile.id}>
              {profile.id === DEFAULT_HARNESS_PROFILE_ID
                ? t("harnessProfile.defaultOptionLabel")
                : profile.name}
            </option>
          ))}
        </select>
      </div>
      {error ? <span className="session-selector-error" title={error}>!</span> : null}
    </div>
  );
}
