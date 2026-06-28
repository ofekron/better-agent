import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { Select } from "./Select";
import { API } from "../api";
import { trackPromise } from "../progress/store";

/** Toggle for `context_strategy` (user_prefs). Controls what happens when a
 * session's context window is exceeded:
 * - "native_compact" (default): let the CLI auto-compact the context
 * - "continuation": start a fresh provider subprocess chained to the previous
 *   one; the agent gathers any needed prior context itself via its tools */
export function ContextStrategySetting() {
  const { t } = useTranslation();
  const [strategy, setStrategy] = useState("native_compact");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    trackPromise("contextStrategy:load", () => fetch(`${API}/api/user-prefs`))
      .promise
      .then((r: Response) => r.json())
      .then((data: { context_strategy?: string }) => {
        setStrategy(data.context_strategy || "native_compact");
      })
      .catch(() => {});
  }, []);

  const change = async (next: string) => {
    setSaving(true);
    try {
      await trackPromise(
        "contextStrategy:save",
        () => fetch(`${API}/api/user-prefs`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ context_strategy: next }),
        }),
      ).promise;
    } catch {
      return;
    } finally {
      setSaving(false);
    }
    setStrategy(next);
  };

  return (
    <div className="context-strategy-setting">
      <label className="context-strategy-row">
        <span>{t("settings.contextStrategy")}</span>
        <Select
          value={strategy}
          disabled={saving}
          onChange={(v) => void change(v)}
          options={[
            { value: "native_compact", label: t("settings.contextStrategyNative") },
            { value: "continuation", label: t("settings.contextStrategyContinuation") },
          ]}
        />
      </label>
      <div className="context-strategy-hint">
        {t("settings.contextStrategyHint")}
      </div>
    </div>
  );
}
