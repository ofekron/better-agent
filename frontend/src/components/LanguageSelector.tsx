import { useTranslation } from "react-i18next";
import { trackedFetch } from "../progress/store";
import { API } from "../api";

const LANGUAGES = [
  { code: "en", labelKey: "language.en" },
  { code: "he", labelKey: "language.he" },
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
      aria-label="Language"
    >
      {LANGUAGES.map((lang) => (
        <option key={lang.code} value={lang.code}>
          {t(lang.labelKey)}
        </option>
      ))}
    </select>
  );
}
