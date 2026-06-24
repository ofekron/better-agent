import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { trackPromise } from "../progress/store";

const TEAM_ORCHESTRATION_API = `${API}/api/extensions/ofek-dev.team-orchestration/backend`;

/** Dropdown for the global `delegate_task_policy`. Controls how the
 * `delegate_task` tool routes a delegated task:
 * - "auto": search for a fitting session (or create one), no approval
 * - "manual": same, but require approval before dispatch
 * - "always_new": skip search, always create a fresh session
 * - "always_new_approve": always create + require approval */
export function DelegateTaskPolicySetting() {
  const { t } = useTranslation();
  const [policy, setPolicy] = useState("auto");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    trackPromise("delegateTaskPolicy:load", () =>
      fetch(`${TEAM_ORCHESTRATION_API}/settings/delegate-task-policy`),
    )
      .promise
      .then((r: Response) => r.json())
      .then((data: { policy?: string }) => {
        setPolicy(data.policy || "auto");
      })
      .catch(() => {});
  }, []);

  const change = async (next: string) => {
    setSaving(true);
    try {
      await trackPromise(
        "delegateTaskPolicy:save",
        () => fetch(`${TEAM_ORCHESTRATION_API}/settings/delegate-task-policy`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ policy: next }),
        }),
      ).promise;
    } catch {
      return;
    } finally {
      setSaving(false);
    }
    setPolicy(next);
  };

  return (
    <div className="delegate-task-policy-setting">
      <label className="context-strategy-row">
        <span>{t("settings.delegateTaskPolicy")}</span>
        <select value={policy} disabled={saving} onChange={(e) => void change(e.target.value)}>
          <option value="auto">{t("settings.delegateTaskPolicyAuto")}</option>
          <option value="manual">{t("settings.delegateTaskPolicyManual")}</option>
          <option value="always_new">{t("settings.delegateTaskPolicyAlwaysNew")}</option>
          <option value="always_new_approve">{t("settings.delegateTaskPolicyAlwaysNewApprove")}</option>
        </select>
      </label>
      <div className="context-strategy-hint">{t("settings.delegateTaskPolicyHint")}</div>
    </div>
  );
}
