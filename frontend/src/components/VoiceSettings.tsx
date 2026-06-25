import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { trackPromise } from "../progress/store";

/** Settings for vocal mode. Backed by the `voice_close_on_background`
 * user pref: when on, vocal mode disables itself the moment the app goes
 * to the background, so the mic is not left listening unintentionally. */
export function VoiceSettings() {
  const { t } = useTranslation();
  const [closeOnBackground, setCloseOnBackground] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    trackPromise("voice:load", () => fetch(`${API}/api/user-prefs`))
      .promise
      .then((r: Response) => r.json())
      .then((data: { voice_close_on_background?: unknown }) => {
        if (typeof data.voice_close_on_background === "boolean") {
          setCloseOnBackground(data.voice_close_on_background);
        }
      })
      .catch(() => {});
  }, []);

  const patch = async (checked: boolean) => {
    setSaving(true);
    try {
      await trackPromise(
        "voice:save",
        () => fetch(`${API}/api/user-prefs`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ voice_close_on_background: checked }),
        }),
      ).promise;
    } catch {
      return;
    } finally {
      setSaving(false);
    }
    setCloseOnBackground(checked);
  };

  return (
    <div className="context-strategy-setting">
      <label className="context-strategy-row">
        <span>{t("settings.voiceCloseOnBackground")}</span>
        <input
          type="checkbox"
          checked={closeOnBackground}
          disabled={saving}
          onChange={(e) => void patch(e.target.checked)}
        />
      </label>
    </div>
  );
}
