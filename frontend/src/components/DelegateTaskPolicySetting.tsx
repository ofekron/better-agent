import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { Select } from "./Select";
import { extBackendBase } from "../extensionIds";
import { runThreeStateSync, trackPromise } from "../progress/store";

const teamOrchestrationApi = () => extBackendBase("team");

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
      fetch(`${teamOrchestrationApi()}/settings/delegate-task-policy`),
    )
      .promise
      .then((r: Response) => r.json())
      .then((data: { policy?: string }) => {
        setPolicy(data.policy || "auto");
      })
      .catch(() => {});
  }, []);

  const change = async (next: string) => {
    const previous = policy;
    setPolicy(next);
    setSaving(true);
    try {
      await runThreeStateSync({
        operationId: "delegateTaskPolicy:save",
        action: t("settings.delegateTaskPolicy"),
        reconcile: async () => {
          const response = await fetch(`${teamOrchestrationApi()}/settings/delegate-task-policy`);
          if (!response.ok) { setPolicy(previous); return; }
          const data = await response.json() as { policy?: string };
          setPolicy(data.policy || "auto");
        },
        mutate: async () => {
          const response = await fetch(`${teamOrchestrationApi()}/settings/delegate-task-policy`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ policy: next }),
          });
          if (!response.ok) throw new Error(await response.text());
          return response;
        },
      });
    } catch {
      return;
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="delegate-task-policy-setting">
      <label className="context-strategy-row">
        <span>{t("settings.delegateTaskPolicy")}</span>
        <Select
          value={policy}
          disabled={saving}
          onChange={(v) => void change(v)}
          options={[
            { value: "auto", label: t("settings.delegateTaskPolicyAuto") },
            { value: "manual", label: t("settings.delegateTaskPolicyManual") },
            { value: "always_new", label: t("settings.delegateTaskPolicyAlwaysNew") },
            { value: "always_new_approve", label: t("settings.delegateTaskPolicyAlwaysNewApprove") },
          ]}
        />
      </label>
      <div className="context-strategy-hint">{t("settings.delegateTaskPolicyHint")}</div>
    </div>
  );
}
