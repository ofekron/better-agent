import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { eventBus } from "../lib/eventBus";
import { trackedFetch } from "../progress/store";
import type { HarnessProfile } from "../types";
import Icon from "./Icon";

interface Props {
  value?: string;
  disabled?: boolean;
  className?: string;
  onChange: (profileId: string, revision: string) => void;
}

const loadOp = "harnessProfiles:list";
const packageOp = "harnessProfiles:packageCurrent";

export function HarnessProfileSelector({
  value = "",
  disabled = false,
  className = "session-model-picker-field",
  onChange,
}: Props) {
  const { t } = useTranslation();
  const [profiles, setProfiles] = useState<HarnessProfile[]>([]);
  const [error, setError] = useState("");
  const [packaging, setPackaging] = useState(false);

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

  const selectedRevision = useMemo(
    () => profiles.find((profile) => profile.id === value)?.revision ?? "",
    [profiles, value],
  );

  const packageCurrent = () => {
    setPackaging(true);
    setError("");
    trackedFetch(packageOp, `${API}/api/harness-profiles/package-current`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<HarnessProfile>;
      })
      .then((profile) => {
        setProfiles((current) => {
          const next = current.filter((item) => item.id !== profile.id);
          next.push(profile);
          return next.sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
        });
        onChange(profile.id, profile.revision);
        setError("");
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => setPackaging(false));
  };

  return (
    <div className={className}>
      <span>{t("harnessProfile.label", "Harness profile")}</span>
      <div className="harness-profile-selector-row">
        <select
          aria-label={t("harnessProfile.label", "Harness profile")}
          value={value}
          disabled={disabled || packaging}
          onChange={(event) => {
            const profileId = event.target.value;
            const profile = profiles.find((item) => item.id === profileId);
            onChange(profileId, profile?.revision ?? "");
          }}
        >
          <option value="">{t("harnessProfile.inherit", "Current harness")}</option>
          {profiles.map((profile) => (
            <option key={profile.id} value={profile.id}>
              {profile.name}
              {profile.base_mode === "bare" ? ` - ${t("harnessProfile.bare", "Bare")}` : ""}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="harness-profile-package-button"
          disabled={disabled || packaging}
          onClick={packageCurrent}
          title={t("harnessProfile.packageCurrent", "Package current harness")}
          aria-label={t("harnessProfile.packageCurrent", "Package current harness")}
        >
          {packaging ? "…" : <Icon name="folder-plus" size={14} />}
        </button>
      </div>
      {error ? <span className="session-selector-error" title={error}>!</span> : null}
      {value && selectedRevision ? (
        <span className="harness-profile-revision">{selectedRevision}</span>
      ) : null}
    </div>
  );
}
