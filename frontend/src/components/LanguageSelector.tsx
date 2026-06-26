import { useTranslation } from "react-i18next";
import { trackedFetch } from "../progress/store";
import { API } from "../api";

// Each option shows its autonym (native name) so a user who switched to a
// language they don't read can still find their own. These are intentionally
// language-independent — never run through t().
const LANGUAGES = [
  { code: "en", autonym: "English" },
  { code: "he", autonym: "עברית" },
  { code: "es", autonym: "Español" },
  { code: "fr", autonym: "Français" },
  { code: "de", autonym: "Deutsch" },
  { code: "pt", autonym: "Português" },
  { code: "it", autonym: "Italiano" },
  { code: "ru", autonym: "Русский" },
  { code: "zh", autonym: "中文" },
  { code: "ja", autonym: "日本語" },
  { code: "ko", autonym: "한국어" },
  { code: "ar", autonym: "العربية" },
  { code: "hi", autonym: "हिन्दी" },
  { code: "nl", autonym: "Nederlands" },
] as const;

export function LanguageSelector() {
  const { t, i18n } = useTranslation();
  const current = i18n.language;

  return (
    <select
      className="language-selector"
      value={current}
      onChange={(e) => {
        const lng = e.target.value;
        i18n.changeLanguage(lng);
        trackedFetch(
          "language:save",
          `${API}/api/user-prefs`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ language: lng }),
          },
          { silent: true },
        ).catch(() => {});
      }}
      aria-label={t("language.label", "Language")}
    >
      {LANGUAGES.map((lang) => (
        <option key={lang.code} value={lang.code}>
          {lang.autonym}
        </option>
      ))}
    </select>
  );
}
