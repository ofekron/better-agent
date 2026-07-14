import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Select } from "./Select";
import { API } from "../api";
import { runThreeStateSync, trackPromise } from "../progress/store";
import { DEFAULT_APP_FONT_SIZE, fontScaleForSize } from "../utils/typography";

export type FontFamilyId = "system" | "serif" | "mono" | "inter";

export interface AppearancePrefs {
  font_family: FontFamilyId;
  font_size: number;
}

export const DEFAULT_APPEARANCE: AppearancePrefs = {
  font_family: "system",
  font_size: DEFAULT_APP_FONT_SIZE,
};

export const FONT_SIZE_MIN = 11;
export const FONT_SIZE_MAX = 20;

const FONT_STACKS: Record<FontFamilyId, string> = {
  system: '-apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif',
  serif: 'Georgia, "Times New Roman", Times, serif',
  mono: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
  inter: 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif',
};

const FONT_OPTIONS: FontFamilyId[] = ["system", "inter", "serif", "mono"];

export function applyAppearancePrefs(prefs: Partial<AppearancePrefs> = {}) {
  const family = prefs.font_family ?? DEFAULT_APPEARANCE.font_family;
  const size = prefs.font_size ?? DEFAULT_APPEARANCE.font_size;
  const root = document.documentElement;
  root.style.setProperty("--app-font-family", FONT_STACKS[family]);
  root.style.setProperty("--app-font-size", `${size}px`);
  root.style.setProperty("--app-font-scale", String(fontScaleForSize(size)));
}

export function AppearanceSetting() {
  const { t } = useTranslation();
  const [prefs, setPrefs] = useState<AppearancePrefs>(DEFAULT_APPEARANCE);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    trackPromise("appearance:load", () => fetch(`${API}/api/user-prefs`))
      .promise
      .then((r: Response) => r.json())
      .then((data: Partial<AppearancePrefs>) => {
        const next = normalizeAppearancePrefs(data);
        setPrefs(next);
        applyAppearancePrefs(next);
      })
      .catch(() => {});
  }, []);

  const save = async (patch: Partial<AppearancePrefs>) => {
    const previous = prefs;
    const next = normalizeAppearancePrefs({ ...prefs, ...patch });
    setPrefs(next);
    applyAppearancePrefs(next);
    setSaving(true);
    try {
      await runThreeStateSync({
        operationId: "appearance:save",
        action: t("settings.appearance"),
        reconcile: async () => {
          const response = await fetch(`${API}/api/user-prefs`);
          if (!response.ok) { setPrefs(previous); applyAppearancePrefs(previous); return; }
          const authoritative = normalizeAppearancePrefs(await response.json());
          setPrefs(authoritative);
          applyAppearancePrefs(authoritative);
        },
        mutate: async () => {
          const response = await fetch(`${API}/api/user-prefs`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(next),
          });
          if (!response.ok) throw new Error(await response.text());
          return response;
        },
      });
      window.dispatchEvent(new CustomEvent("appearance_prefs_changed", { detail: next }));
    } catch {
      return;
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="appearance-setting">
      <label className="appearance-setting-row">
        <span>{t("settings.fontFamily")}</span>
        <Select<FontFamilyId>
          value={prefs.font_family}
          disabled={saving}
          onChange={(v) => void save({ font_family: v })}
          options={FONT_OPTIONS.map((option) => ({
            value: option,
            label: t(`settings.fontFamily.${option}`),
          }))}
        />
      </label>
      <label className="appearance-setting-row">
        <span>{t("settings.fontSize")}</span>
        <input
          type="number"
          min={FONT_SIZE_MIN}
          max={FONT_SIZE_MAX}
          step="1"
          inputMode="numeric"
          value={prefs.font_size}
          disabled={saving}
          onChange={(e) => void save({ font_size: Number(e.target.value) })}
        />
      </label>
    </div>
  );
}

function normalizeAppearancePrefs(data: Partial<AppearancePrefs>): AppearancePrefs {
  const fontFamily = FONT_OPTIONS.includes(data.font_family as FontFamilyId)
    ? data.font_family as FontFamilyId
    : DEFAULT_APPEARANCE.font_family;
  const maybeFontSize = data.font_size;
  const fontSize = typeof maybeFontSize === "number"
    && Number.isInteger(maybeFontSize)
    && maybeFontSize >= FONT_SIZE_MIN
    && maybeFontSize <= FONT_SIZE_MAX
    ? maybeFontSize
    : DEFAULT_APPEARANCE.font_size;
  return { font_family: fontFamily, font_size: fontSize };
}
