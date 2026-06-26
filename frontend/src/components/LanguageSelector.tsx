import { useTranslation } from "react-i18next";
import { trackedFetch } from "../progress/store";
import { API } from "../api";

const LANGUAGES = [
  { code: "en", labelKey: "language.en" },
  { code: "he", labelKey: "language.he" },
  { code: "es", labelKey: "language.es" },
  { code: "fr", labelKey: "language.fr" },
  { code: "de", labelKey: "language.de" },
  { code: "pt", labelKey: "language.pt" },
  { code: "it", labelKey: "language.it" },
  { code: "ru", labelKey: "language.ru" },
  { code: "zh", labelKey: "language.zh" },
  { code: "ja", labelKey: "language.ja" },
  { code: "ko", labelKey: "language.ko" },
  { code: "ar", labelKey: "language.ar" },
  { code: "hi", labelKey: "language.hi" },
  { code: "nl", labelKey: "language.nl" },
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
          {t(lang.labelKey)}
        </option>
      ))}
    </select>
  );
}
