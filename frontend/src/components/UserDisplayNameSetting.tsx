import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { trackPromise } from "../progress/store";

const MAX_USER_DISPLAY_NAME_LENGTH = 80;

export function UserDisplayNameSetting() {
  const { t } = useTranslation();
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    trackPromise("userDisplayName:load", () => fetch(`${API}/api/user-prefs`))
      .promise
      .then((r: Response) => r.json())
      .then((data: { user_display_name?: unknown }) => {
        setValue(typeof data.user_display_name === "string" ? data.user_display_name : "");
      })
      .catch(() => {});
  }, []);

  const save = async () => {
    const next = value.trim().replace(/\s+/g, " ");
    setValue(next);
    setSaving(true);
    try {
      const response = await trackPromise(
        "userDisplayName:save",
        () => fetch(`${API}/api/user-prefs`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user_display_name: next || null }),
        }),
      ).promise;
      if (!response.ok) throw new Error(await response.text());
      const prefs = await response.json() as { user_display_name?: unknown };
      setValue(typeof prefs.user_display_name === "string" ? prefs.user_display_name : next);
      window.dispatchEvent(new CustomEvent("user_prefs_changed", { detail: prefs }));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="user-display-name-setting">
      <label className="user-display-name-setting-row">
        <span>{t("settings.userDisplayName")}</span>
        <input
          type="text"
          maxLength={MAX_USER_DISPLAY_NAME_LENGTH}
          value={value}
          disabled={saving}
          placeholder={t("settings.userDisplayNamePlaceholder")}
          onChange={(e) => setValue(e.target.value)}
          onBlur={() => void save()}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.currentTarget.blur();
            }
          }}
        />
      </label>
    </div>
  );
}
