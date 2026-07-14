import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { runThreeStateSync, trackPromise } from "../progress/store";

/** Toggle for `auto_restart_on_idle` (user_prefs). When ON, the backend
 * auto-fires a supervisor restart every time the system goes idle after
 * work, so code changes are picked up without a manual "Refresh". Only
 * has effect under the run.sh supervisor. Default OFF. */
export function AutoRestartOnIdleSetting() {
  const { t } = useTranslation();
  const [enabled, setEnabled] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    trackPromise("autoRestart:load", () => fetch(`${API}/api/user-prefs`))
      .promise
      .then((r: Response) => r.json())
      .then((data: { auto_restart_on_idle?: boolean }) => {
        setEnabled(!!data.auto_restart_on_idle);
      })
      .catch(() => {});
  }, []);

  const toggle = async (next: boolean) => {
    const previous = enabled;
    setEnabled(next);
    setSaving(true);
    try {
      await runThreeStateSync({
        operationId: "autoRestart:save",
        action: t("settings.autoRestartOnIdle"),
        reconcile: async () => {
          const response = await fetch(`${API}/api/user-prefs`);
          if (!response.ok) { setEnabled(previous); return; }
          const prefs = await response.json() as { auto_restart_on_idle?: boolean };
          setEnabled(!!prefs.auto_restart_on_idle);
        },
        mutate: async () => {
          const response = await fetch(`${API}/api/user-prefs`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ auto_restart_on_idle: next }),
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
    <div className="auto-restart-setting">
      <label className="auto-restart-row">
        <input
          type="checkbox"
          checked={enabled}
          disabled={saving}
          onChange={(e) => void toggle(e.target.checked)}
        />
        <span>{t("settings.autoRestartOnIdle")}</span>
      </label>
      <div className="auto-restart-hint">
        {t("settings.autoRestartOnIdleHint")}
      </div>
    </div>
  );
}
