import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { Select } from "./Select";
import { API } from "../api";
import { runThreeStateSync, trackPromise } from "../progress/store";

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
    const previous = strategy;
    setStrategy(next);
    setSaving(true);
    try {
      await runThreeStateSync({
        operationId: "contextStrategy:save",
        action: t("settings.contextStrategy"),
        reconcile: async () => {
          const response = await fetch(`${API}/api/user-prefs`);
          if (!response.ok) { setStrategy(previous); return; }
          const prefs = await response.json() as { context_strategy?: string };
          setStrategy(prefs.context_strategy || "native_compact");
        },
        mutate: async () => {
          const response = await fetch(`${API}/api/user-prefs`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ context_strategy: next }),
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
