import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { SurfaceClient } from "../adapter/client";
import type { InstallationCapabilityWire } from "../adapter/wire";
import { trackPromise } from "../progress/store";
import { subscribeSystemFrames, submitSystemIntent } from "../lib/systemFeedRegistry";

/** What setup started with is a starting point, not a ceiling: every
 * capability can be turned on later here. A capability the running process was
 * not started with reports `restart_required` rather than pretending to be
 * live — subsystems are wired once at boot from a value that cannot move under
 * them.
 *
 * ADR 0011 §8 — fully v2: `GET /api/v2/surface/installation-capabilities`
 * backfills on mount, the `installation_capability_changed` feed applies
 * live deltas, `set_installation_capability` mutates native-when-open with
 * the legacy `PATCH` route as fallback (same "native-when-open, legacy
 * fallback" gating every other Package C mutation uses). `setup_required`
 * is read off the v2 list itself rather than a separate status endpoint:
 * `SystemSurfaceAdapter.list_installation_capabilities` honestly returns
 * an empty tuple until the installation profile is bootstrapped (see
 * `backend/scripts/test_adapter_system.py`'s own coverage of that), so an
 * empty result after the first load is exactly that state, not a
 * fabricated inference. */

type CapabilityId = "mobile" | "integrations";

const systemClient = new SurfaceClient();

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
  const [capabilities, setCapabilities] = useState<Record<string, InstallationCapabilityWire>>({});
  const [loaded, setLoaded] = useState(false);
  const [pending, setPending] = useState<CapabilityId | null>(null);
  const [error, setError] = useState("");

  const refetch = useCallback(async () => {
    try {
      const envelope = await trackPromise(
        "installation-capabilities:load",
        () => systemClient.fetchInstallationCapabilities(),
      ).promise;
      if (envelope.kind !== "ok") throw new Error(envelope.kind);
      setCapabilities((prev) => {
        const next = { ...prev };
        for (const capability of envelope.value) next[capability.capability_id] = capability;
        return next;
      });
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "load failed");
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void refetch();
  }, [refetch]);

  useEffect(() => {
    return subscribeSystemFrames((frame) => {
      if (frame.type !== "installation_capability_changed") return;
      setCapabilities((prev) => ({ ...prev, [frame.capability.capability_id]: frame.capability }));
    });
  }, []);

  const setEnabled = async (capability: CapabilityId, enabled: boolean) => {
    setPending(capability);
    setError("");
    // Mirrors the legacy route's own condition exactly: disabling
    // integrations always needs the safety confirmation, whichever
    // transport carries the mutation — the browser `confirm()` dialog
    // (`confirmDisable` below) is the user-facing half, this is the
    // server-facing acknowledgement it already happened.
    const confirm = capability === "integrations" && !enabled;
    try {
      const native = submitSystemIntent({
        kind: "set_installation_capability",
        capability_id: capability,
        enabled,
        confirm_cancels_extension_work: confirm,
      });
      if (native) {
        const ack = await native;
        if (ack.type === "intent_rejected") throw new Error(ack.message || ack.code);
      } else {
        const response = await trackPromise(
          `installation-capabilities:${capability}`,
          () => fetch(`${API}/api/installation-profile/capabilities/${capability}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              enabled,
              ...(confirm ? { confirm_cancels_extension_work: true } : {}),
            }),
          }),
        ).promise;
        if (!response.ok) {
          const detail = (await response.json().catch(() => null)) as { detail?: string } | null;
          throw new Error(detail?.detail || `HTTP ${response.status}`);
        }
      }
      // Neither transport hands the new state back synchronously (the
      // intent ack is accept/reject-only; the legacy fallback's response
      // body is the OLD `InstallationProfile` shape, not this component's
      // v2 read model) — re-pull the authoritative v2 list rather than
      // trust either response body.
      await refetch();
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

  if (loaded && !error && Object.keys(capabilities).length === 0) {
    return <div className="capability-settings-empty">{t("settings.capabilitySetupRequired")}</div>;
  }

  return (
    <div className="capability-settings">
      <p className="capability-settings-intro">{t("settings.capabilityIntro")}</p>
      {CAPABILITIES.map(({ id, label, hint }) => {
        const state = capabilities[id];
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
