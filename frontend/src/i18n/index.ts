import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import en from "./en.json";
import he from "./he.json";
import es from "./es.json";
import fr from "./fr.json";
import de from "./de.json";
import pt from "./pt.json";
import it from "./it.json";
import ru from "./ru.json";
import zh from "./zh.json";
import ja from "./ja.json";
import ko from "./ko.json";
import ar from "./ar.json";
import hi from "./hi.json";
import nl from "./nl.json";

const RTL_LANGUAGES = new Set(["he", "ar"]);

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      en: { translation: en },
      he: { translation: he },
      es: { translation: es },
      fr: { translation: fr },
      de: { translation: de },
      pt: { translation: pt },
      it: { translation: it },
      ru: { translation: ru },
      zh: { translation: zh },
      ja: { translation: ja },
      ko: { translation: ko },
      ar: { translation: ar },
      hi: { translation: hi },
      nl: { translation: nl },
    },
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
