import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { trackPromise } from "../progress/store";

type GuardKey =
  | "sync_wait_depth_cap"
  | "session_creation_depth_cap"
  | "session_max_live_descendants";

const GUARDS: {
  key: GuardKey;
  labelKey: string;
  hintKey: string;
  min: number;
  max: number;
  fallback: number;
}[] = [
  {
    key: "sync_wait_depth_cap",
    labelKey: "settings.syncWaitDepthCap",
    hintKey: "settings.syncWaitDepthCapHint",
    min: 0,
    max: 20,
    fallback: 3,
  },
  {
    key: "session_creation_depth_cap",
    labelKey: "settings.sessionCreationDepthCap",
    hintKey: "settings.sessionCreationDepthCapHint",
    min: 0,
    max: 50,
    fallback: 5,
  },
  {
    key: "session_max_live_descendants",
    labelKey: "settings.sessionMaxLiveDescendants",
    hintKey: "settings.sessionMaxLiveDescendantsHint",
    min: 0,
    max: 200,
    fallback: 12,
  },
];

export function RecursionGuardsSettings() {
  const { t } = useTranslation();
  const [values, setValues] = useState<Record<GuardKey, number>>({
    sync_wait_depth_cap: 3,
    session_creation_depth_cap: 5,
    session_max_live_descendants: 12,
  });
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    trackPromise("recursionGuards:load", () => fetch(`${API}/api/user-prefs`))
      .promise.then((response: Response) => response.json())
      .then((prefs: Partial<Record<GuardKey, number>>) => {
        setValues((current) => {
          const next = { ...current };
          for (const guard of GUARDS) {
            const value = prefs[guard.key];
            if (typeof value === "number") next[guard.key] = value;
          }
          return next;
        });
      })
      .catch(() => {});
  }, []);

  const save = async (guard: (typeof GUARDS)[number], raw: number) => {
    const bounded = Number.isFinite(raw)
      ? Math.min(guard.max, Math.max(guard.min, Math.round(raw)))
      : guard.fallback;
    setValues((current) => ({ ...current, [guard.key]: bounded }));
    setSaving(true);
    try {
      const response = await trackPromise("recursionGuards:save", () =>
        fetch(`${API}/api/user-prefs`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ [guard.key]: bounded }),
        }),
      ).promise;
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="recursion-guards-settings">
      <h3>{t("settings.recursionGuardsTitle")}</h3>
      <p className="recursion-guards-settings__hint">
        {t("settings.recursionGuardsHint")}
      </p>
      {GUARDS.map((guard) => (
        <label key={guard.key} className="task-start-silence-setting">
          <span>{t(guard.labelKey)}</span>
          <span className="task-start-silence-setting__control">
            <input
              type="number"
              min={guard.min}
              max={guard.max}
              value={values[guard.key]}
              disabled={saving}
              onChange={(event) =>
                setValues((current) => ({
                  ...current,
                  [guard.key]: Number(event.target.value),
                }))
              }
              onBlur={() => void save(guard, values[guard.key])}
              aria-describedby={`${guard.key}-hint`}
            />
          </span>
          <span id={`${guard.key}-hint`} className="task-start-silence-setting__hint">
            {t(guard.hintKey)}
          </span>
        </label>
      ))}
    </div>
  );
}
