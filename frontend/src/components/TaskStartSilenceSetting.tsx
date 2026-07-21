import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { trackPromise } from "../progress/store";

const MIN_SECONDS = 15;
const MAX_SECONDS = 3600;

export function TaskStartSilenceSetting() {
  const { t } = useTranslation();
  const [seconds, setSeconds] = useState(90);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    trackPromise("taskStartSilence:load", () => fetch(`${API}/api/user-prefs`))
      .promise.then((response: Response) => response.json())
      .then((prefs: { task_start_silence_seconds?: number }) => {
        if (typeof prefs.task_start_silence_seconds === "number") {
          setSeconds(prefs.task_start_silence_seconds);
        }
      })
      .catch(() => {});
  }, []);

  const save = async (next: number) => {
    const bounded = Number.isFinite(next)
      ? Math.min(MAX_SECONDS, Math.max(MIN_SECONDS, Math.round(next)))
      : 90;
    setSeconds(bounded);
    setSaving(true);
    try {
      const response = await trackPromise("taskStartSilence:save", () =>
        fetch(`${API}/api/user-prefs`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ task_start_silence_seconds: bounded }),
        }),
      ).promise;
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <label className="task-start-silence-setting">
      <span>{t("settings.taskStartSilence")}</span>
      <span className="task-start-silence-setting__control">
        <input
          type="number"
          min={MIN_SECONDS}
          max={MAX_SECONDS}
          value={seconds}
          disabled={saving}
          onChange={(event) => setSeconds(Number(event.target.value))}
          onBlur={() => void save(seconds)}
          aria-describedby="task-start-silence-hint"
        />
        <span>{t("settings.seconds")}</span>
      </span>
      <span id="task-start-silence-hint" className="task-start-silence-setting__hint">
        {t("settings.taskStartSilenceHint")}
      </span>
    </label>
  );
}
