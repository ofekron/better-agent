import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { runThreeStateSync, trackPromise } from "../progress/store";

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
    const previous = value;
    setValue(nextValue);
    const trimmed = nextValue.trim();
    const days = trimmed === "" ? null : Number(trimmed);
    if (days !== null && (!Number.isInteger(days) || days < 1)) {
      return;
    }
    setSaving(true);
    try {
      await runThreeStateSync({
        operationId: "sessionAutoDelete:save",
        action: t("settings.sessionAutoDelete"),
        reconcile: async () => {
          const response = await fetch(`${API}/api/user-prefs`);
          if (!response.ok) { setValue(previous); return; }
          const prefs = await response.json() as { session_auto_delete_days?: number | null };
          const authoritative = prefs.session_auto_delete_days;
          setValue(typeof authoritative === "number" && authoritative > 0 ? String(authoritative) : "");
        },
        mutate: async () => {
          const response = await fetch(`${API}/api/user-prefs`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_auto_delete_days: days }),
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
