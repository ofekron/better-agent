import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import en from "./en.json";
import he from "./he.json";

const RTL_LANGUAGES = new Set(["he"]);

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: { en: { translation: en }, he: { translation: he } },
    fallbackLng: "en",
    interpolation: { escapeValue: false },
    react: { useSuspense: false },
    detection: {
      order: ["localStorage", "navigator"],
      lookupLocalStorage: "better-agent-language",
      caches: ["localStorage"],
    },
  });

function applyDirection(lng: string) {
  document.documentElement.dir = RTL_LANGUAGES.has(lng) ? "rtl" : "ltr";
  document.documentElement.lang = lng;
}

applyDirection(i18n.language);
i18n.on("languageChanged", applyDirection);

export default i18n;
