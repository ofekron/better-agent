import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { trackPromise } from "../progress/store";
import { eventBus } from "src/lib/eventBus";

/** What setup started with is a starting point, not a ceiling: every
 * capability can be turned on later here. A capability the running process was
 * not started with reports `restart_required` rather than pretending to be
 * live — subsystems are wired once at boot from a value that cannot move under
 * them. */

type CapabilityId = "mobile" | "integrations";

interface CapabilityState {
  enabled: boolean;
  provisioned: boolean;
  active: boolean;
  restart_required: boolean;
  self_provisionable: boolean;
  in_app_restart_supported: boolean;
}

interface InstallationProfile {
  setup_required?: boolean;
  mode?: string | null;
  capabilities?: Record<string, CapabilityState>;
}

const CAPABILITIES: { id: CapabilityId; label: string; hint: string }[] = [
  {
    id: "integrations",
    label: "settings.capabilityIntegrations",
    hint: "settings.capabilityIntegrationsHint",
  },
  { id: "mobile", label: "settings.capabilityMobile", hint: "settings.capabilityMobileHint" },
];

export function InstallationCapabilities({
  onRestartRequested,
}: {
  onRestartRequested?: () => void;
}) {
  const { t } = useTranslation();
  const [profile, setProfile] = useState<InstallationProfile | null>(null);
  const [pending, setPending] = useState<CapabilityId | null>(null);
  const [error, setError] = useState("");

  const refetch = useCallback(async () => {
    try {
      const response = await trackPromise(
        "installation-capabilities:load",
        () => fetch(`${API}/api/installation-profile`),
      ).promise;
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setProfile((await response.json()) as InstallationProfile);
    } catch (e) {
      setError(e instanceof Error ? e.message : "load failed");
    }
  }, []);

  useEffect(() => {
    void refetch();
    return eventBus.subscribe(
      "installation_capabilities_changed",
      () => void refetch(),
    );
  }, [refetch]);

  const setEnabled = async (capability: CapabilityId, enabled: boolean) => {
    setPending(capability);
    setError("");
    try {
      const response = await trackPromise(
        `installation-capabilities:${capability}`,
        () => fetch(`${API}/api/installation-profile/capabilities/${capability}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            enabled,
            ...(capability === "integrations" && !enabled
              ? { confirm_cancels_extension_work: true }
              : {}),
          }),
        }),
      ).promise;
      if (!response.ok) {
        const detail = (await response.json().catch(() => null)) as { detail?: string } | null;
        throw new Error(detail?.detail || `HTTP ${response.status}`);
      }
      setProfile((await response.json()) as InstallationProfile);
    } catch (e) {
      setError(e instanceof Error ? e.message : "update failed");
    } finally {
      setPending(null);
    }
  };

  const confirmDisable = (capability: CapabilityId) => {
    if (capability !== "integrations") return true;
    return window.confirm(t("settings.capabilityIntegrationsDisableWarning"));
  };

  if (profile?.setup_required) {
    return <div className="capability-settings-empty">{t("settings.capabilitySetupRequired")}</div>;
  }

  return (
    <div className="capability-settings">
      <p className="capability-settings-intro">{t("settings.capabilityIntro")}</p>
      {CAPABILITIES.map(({ id, label, hint }) => {
        const state = profile?.capabilities?.[id];
        const busy = pending === id;
        return (
          <div className="capability-row" key={id}>
            <label className="capability-row-main">
              <span className="capability-row-text">
                <span className="capability-row-label">{t(label)}</span>
                <span className="capability-row-hint">{t(hint)}</span>
              </span>
              <input
                type="checkbox"
                checked={state?.enabled === true}
                disabled={!state || busy}
                onChange={(e) => {
                  if (!e.target.checked && !confirmDisable(id)) return;
                  void setEnabled(id, e.target.checked);
                }}
              />
            </label>
            <div className="capability-row-status" aria-live="polite">
              {busy && <span className="capability-badge is-busy">{t("settings.capabilitySaving")}</span>}
              {!busy && state?.enabled && !state.provisioned && !state.self_provisionable && (
                <span className="capability-badge is-blocked">
                  {t("settings.capabilityNeedsBuild")}
                </span>
              )}
              {!busy && state?.restart_required && state.in_app_restart_supported && (
                <button
                  type="button"
                  className="capability-badge is-restart"
                  onClick={() => onRestartRequested?.()}
                >
                  {t("settings.capabilityRestartRequired")}
                </button>
              )}
              {!busy && state?.restart_required && !state.in_app_restart_supported && (
                <span className="capability-badge is-restart">
                  {t("settings.capabilityRestartManually")}
                </span>
              )}
              {!busy && !state?.restart_required && state?.active && (
                <span className="capability-badge is-active">{t("settings.capabilityActive")}</span>
              )}
            </div>
          </div>
        );
      })}
      {error && <div className="capability-settings-error">{error}</div>}
    </div>
  );
}
