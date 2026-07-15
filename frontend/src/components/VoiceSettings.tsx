import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { runThreeStateSync, trackPromise } from "../progress/store";

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
    const previous = closeOnBackground;
    setCloseOnBackground(checked);
    setSaving(true);
    try {
      await runThreeStateSync({
        operationId: "voice:save",
        action: t("settings.voiceCloseOnBackground"),
        reconcile: async () => {
          const response = await fetch(`${API}/api/user-prefs`);
          if (!response.ok) { setCloseOnBackground(previous); return; }
          const prefs = await response.json() as { voice_close_on_background?: unknown };
          if (typeof prefs.voice_close_on_background === "boolean") setCloseOnBackground(prefs.voice_close_on_background);
        },
        mutate: async () => {
          const response = await fetch(`${API}/api/user-prefs`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ voice_close_on_background: checked }),
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
