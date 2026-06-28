import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { trackPromise } from "../progress/store";

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
    setSaving(true);
    try {
      await trackPromise(
        "autoRestart:save",
        () => fetch(`${API}/api/user-prefs`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ auto_restart_on_idle: next }),
        }),
      ).promise;
    } catch {
      return;
    } finally {
      setSaving(false);
    }
    setEnabled(next);
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
