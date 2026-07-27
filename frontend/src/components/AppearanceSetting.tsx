import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Select } from "./Select";
import { API } from "../api";
import { trackPromise } from "../progress/store";
import { DEFAULT_APP_FONT_SIZE, fontScaleForSize } from "../utils/typography";

export type FontFamilyId = "system" | "serif" | "mono" | "inter";
export type AppearanceThemeId = "default" | "nord" | "dracula";

export interface AppearancePrefs {
  font_family: FontFamilyId;
  font_size: number;
  appearance_theme: AppearanceThemeId;
}

export const DEFAULT_APPEARANCE: AppearancePrefs = {
  font_family: "system",
  font_size: DEFAULT_APP_FONT_SIZE,
  appearance_theme: "default",
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
const THEME_OPTIONS: AppearanceThemeId[] = ["default", "nord", "dracula"];

export function applyAppearancePrefs(prefs: Partial<AppearancePrefs> = {}) {
  const family = prefs.font_family ?? DEFAULT_APPEARANCE.font_family;
  const size = prefs.font_size ?? DEFAULT_APPEARANCE.font_size;
  const root = document.documentElement;
  root.style.setProperty("--app-font-family", FONT_STACKS[family]);
  root.style.setProperty("--app-font-size", `${size}px`);
  root.style.setProperty("--app-font-scale", String(fontScaleForSize(size)));
  root.dataset.theme = THEME_OPTIONS.includes(prefs.appearance_theme as AppearanceThemeId)
    ? prefs.appearance_theme
    : DEFAULT_APPEARANCE.appearance_theme;
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
    const next = normalizeAppearancePrefs({ ...prefs, ...patch });
    setPrefs(next);
    applyAppearancePrefs(next);
    setSaving(true);
    try {
      await trackPromise(
        "appearance:save",
        () => fetch(`${API}/api/user-prefs`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(next),
        }),
      ).promise;
      window.dispatchEvent(new CustomEvent("appearance_prefs_changed", { detail: next }));
    } catch {
      return;
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="appearance-setting">
      <fieldset className="appearance-theme-fieldset" disabled={saving}>
        <legend>{t("settings.theme")}</legend>
        <div className="appearance-theme-grid">
          {THEME_OPTIONS.map((theme) => (
            <label
              className={`appearance-theme-option${prefs.appearance_theme === theme ? " is-selected" : ""}`}
              data-theme-preview={theme}
              key={theme}
            >
              <input
                type="radio"
                name="appearance-theme"
                value={theme}
                checked={prefs.appearance_theme === theme}
                onChange={() => void save({ appearance_theme: theme })}
              />
              <span className="appearance-theme-swatches" aria-hidden="true">
                <i />
                <i />
                <i />
              </span>
              <strong>{t(`settings.theme.${theme}`)}</strong>
            </label>
          ))}
        </div>
      </fieldset>
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
        <span>
          {t("settings.fontSize")}
          <small>{t("settings.fontSizeHint")}</small>
        </span>
        <span className="appearance-font-size-controls">
          <input
            aria-label={t("settings.fontSizeSlider")}
            type="range"
            min={FONT_SIZE_MIN}
            max={FONT_SIZE_MAX}
            step="1"
            value={prefs.font_size}
            disabled={saving}
            onChange={(e) => void save({ font_size: Number(e.target.value) })}
          />
          <input
            aria-label={t("settings.fontSize")}
            type="number"
            min={FONT_SIZE_MIN}
            max={FONT_SIZE_MAX}
            step="1"
            inputMode="numeric"
            value={prefs.font_size}
            disabled={saving}
            onChange={(e) => void save({ font_size: Number(e.target.value) })}
          />
        </span>
      </label>
      <p className="appearance-font-preview">{t("settings.fontPreview")}</p>
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
  const theme = THEME_OPTIONS.includes(data.appearance_theme as AppearanceThemeId)
    ? data.appearance_theme as AppearanceThemeId
    : DEFAULT_APPEARANCE.appearance_theme;
  return { font_family: fontFamily, font_size: fontSize, appearance_theme: theme };
}
