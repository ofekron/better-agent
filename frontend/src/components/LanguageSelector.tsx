import { useTranslation } from "react-i18next";
import { Select } from "./Select";
import { runThreeStateSync } from "../progress/store";
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
    <Select
      value={current}
      onChange={(lng) => {
        const previous = i18n.language;
        void i18n.changeLanguage(lng);
        void runThreeStateSync({
          operationId: "language:save",
          action: t("language.label", "Language"),
          reconcile: async () => {
            const response = await fetch(`${API}/api/user-prefs`);
            if (!response.ok) { await i18n.changeLanguage(previous); return; }
            const prefs = await response.json() as { language?: string };
            await i18n.changeLanguage(prefs.language || previous);
          },
          mutate: async () => {
            const response = await fetch(`${API}/api/user-prefs`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ language: lng }),
            });
            if (!response.ok) throw new Error(await response.text());
            return response;
          },
        }).catch(() => {});
      }}
      aria-label={t("language.label", "Language")}
      options={LANGUAGES.map((lang) => ({ value: lang.code, label: lang.autonym }))}
    />
  );
}
