import { describe, expect, it } from "vitest";
import en from "../src/i18n/en.json";
import ar from "../src/i18n/ar.json";
import de from "../src/i18n/de.json";
import es from "../src/i18n/es.json";
import fr from "../src/i18n/fr.json";
import he from "../src/i18n/he.json";
import hi from "../src/i18n/hi.json";
import italian from "../src/i18n/it.json";
import ja from "../src/i18n/ja.json";
import ko from "../src/i18n/ko.json";
import nl from "../src/i18n/nl.json";
import pt from "../src/i18n/pt.json";
import ru from "../src/i18n/ru.json";
import zh from "../src/i18n/zh.json";

const locales = { ar, de, es, fr, he, hi, it: italian, ja, ko, nl, pt, ru, zh };
const translatableKeys = Object.keys(en).filter(
  (key) => key.startsWith("settings.ambientMcps") && key !== "settings.ambientMcpsField.id",
) as (keyof typeof en)[];

describe("ambient MCP locale coverage", () => {
  for (const [locale, catalog] of Object.entries(locales)) {
    it(`translates user-facing copy in ${locale}`, () => {
      for (const key of translatableKeys) {
        expect(catalog[key], key).toBeTruthy();
        expect(catalog[key], key).not.toBe(en[key]);
      }
    });
  }
});
