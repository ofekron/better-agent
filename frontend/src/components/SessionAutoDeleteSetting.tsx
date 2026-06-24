import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { trackPromise } from "../progress/store";

export function SessionAutoDeleteSetting() {
  const { t } = useTranslation();
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    trackPromise("sessionAutoDelete:load", () => fetch(`${API}/api/user-prefs`))
      .promise
      .then((r: Response) => r.json())
      .then((data: { session_auto_delete_days?: number | null }) => {
        const days = data.session_auto_delete_days;
        setValue(typeof days === "number" && days > 0 ? String(days) : "");
      })
      .catch(() => {});
  }, []);

  const save = async (nextValue: string) => {
    setValue(nextValue);
    const trimmed = nextValue.trim();
    const days = trimmed === "" ? null : Number(trimmed);
    if (days !== null && (!Number.isInteger(days) || days < 1)) {
      return;
    }
    setSaving(true);
    try {
      await trackPromise(
        "sessionAutoDelete:save",
        () => fetch(`${API}/api/user-prefs`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_auto_delete_days: days }),
        }),
      ).promise;
    } catch {
      return;
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="session-auto-delete-setting">
      <label className="session-auto-delete-row">
        <span>{t("settings.sessionAutoDelete")}</span>
        <input
          type="number"
          min="1"
          step="1"
          inputMode="numeric"
          value={value}
          disabled={saving}
          placeholder={t("settings.sessionAutoDeleteNever")}
          onChange={(e) => void save(e.target.value)}
        />
      </label>
      <div className="session-auto-delete-hint">
        {t("settings.sessionAutoDeleteHint")}
      </div>
    </div>
  );
}
