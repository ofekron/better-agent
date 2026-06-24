import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { trackPromise } from "../progress/store";

/** Toggle for `cross_session_delegate_auto` (user_prefs). When ON, a
 * `delegate_to_session` call with `approval:"auto"` AND `run_mode:"fork"`
 * runs WITHOUT the confirmation picker. Default OFF (fail closed): every
 * cross-session delegation is gated through the picker. `continue` always
 * requires the picker regardless of this flag. */
export function CrossSessionDelegateSetting() {
  const { t } = useTranslation();
  const [enabled, setEnabled] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    trackPromise("xsessionDelegate:load", () => fetch(`${API}/api/user-prefs`))
      .promise
      .then((r: Response) => r.json())
      .then((data: { cross_session_delegate_auto?: boolean }) => {
        setEnabled(!!data.cross_session_delegate_auto);
      })
      .catch(() => {});
  }, []);

  const toggle = async (next: boolean) => {
    setSaving(true);
    try {
      await trackPromise(
        "xsessionDelegate:save",
        () => fetch(`${API}/api/user-prefs`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cross_session_delegate_auto: next }),
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
    <div className="xsession-delegate-setting">
      <label className="xsession-delegate-row">
        <input
          type="checkbox"
          checked={enabled}
          disabled={saving}
          onChange={(e) => void toggle(e.target.checked)}
        />
        <span>{t("settings.crossSessionDelegateAuto")}</span>
      </label>
      <div className="xsession-delegate-hint">
        {t("settings.crossSessionDelegateAutoHint")}
      </div>
    </div>
  );
}
