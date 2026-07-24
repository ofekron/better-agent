import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { eventBus } from "../lib/eventBus";
import { trackedFetch } from "../progress/store";
import type { HarnessProfile } from "../types";

interface Props {
  value?: string;
  disabled?: boolean;
  className?: string;
  onChange: (profileId: string, revision: string) => void;
}

const loadOp = "harnessProfiles:list";

export function HarnessProfileSelector({
  value = "default",
  disabled = false,
  className = "session-model-picker-field",
  onChange,
}: Props) {
  const { t } = useTranslation();
  const [profiles, setProfiles] = useState<HarnessProfile[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    const load = () => trackedFetch(loadOp, `${API}/api/harness-profiles`)
      .then((response) => response.json() as Promise<{ profiles?: HarnessProfile[] }>)
      .then((body) => {
        if (cancelled) return;
        setProfiles(body.profiles ?? []);
        setError("");
      })
      .catch((err) => {
        if (cancelled) return;
        setProfiles([]);
        setError(err instanceof Error ? err.message : String(err));
      });
    void load();
    const unsubscribe = eventBus.subscribe("harness_profiles_changed", () => {
      void load();
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, []);

  const effectiveValue = value || "default";

  const selectedRevision = useMemo(
    () => profiles.find((profile) => profile.id === effectiveValue)?.revision ?? "",
    [profiles, effectiveValue],
  );

  return (
    <div className={className}>
      <span>{t("harnessProfile.label", "Harness profile")}</span>
      <div className="harness-profile-selector-row">
        <select
          aria-label={t("harnessProfile.label", "Harness profile")}
          value={effectiveValue}
          disabled={disabled}
          onChange={(event) => {
            const profileId = event.target.value;
            const profile = profiles.find((item) => item.id === profileId);
            onChange(profileId, profile?.revision ?? "");
          }}
        >
          {profiles.length === 0 ? (
            <option value="default">{t("harnessProfile.defaultOptionLabel")}</option>
          ) : null}
          {profiles.map((profile) => (
            <option key={profile.id} value={profile.id}>
              {profile.id === "default" ? t("harnessProfile.defaultOptionLabel") : profile.name}
            </option>
          ))}
        </select>
      </div>
      {error ? <span className="session-selector-error" title={error}>!</span> : null}
      {effectiveValue !== "default" && selectedRevision ? (
        <span className="harness-profile-revision">{selectedRevision}</span>
      ) : null}
    </div>
  );
}
