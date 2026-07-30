import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

// Deterministic i18n drift lock:
// 1. The locale roster is exactly the 14 shipped locales.
// 2. Every locale carries the same key set as en.json (both directions),
//    compared on plural-base keys so locale-specific plural forms
//    (e.g. Russian `_few`/`_many`) are legitimate.
// 3. Every key statically referenced from src (t("...") first-argument
//    string literals, including ternary branches, and <Trans i18nKey>)
//    exists in en.json. Dynamic keys (template literals with ${}) are out
//    of static reach and are not asserted here.

const I18N_DIR = path.resolve(__dirname, "../src/i18n");
const SRC_DIR = path.resolve(__dirname, "../src");

const EXPECTED_LOCALES = [
  "ar", "de", "en", "es", "fr", "he", "hi", "it", "ja", "ko", "nl", "pt", "ru", "zh",
];

type Bundle = Record<string, string>;

function flatten(obj: Record<string, unknown>, prefix = ""): Bundle {
  const out: Bundle = {};
  for (const [key, value] of Object.entries(obj)) {
    const full = prefix ? `${prefix}.${key}` : key;
    if (value && typeof value === "object") {
      Object.assign(out, flatten(value as Record<string, unknown>, full));
    } else {
      out[full] = String(value);
    }
  }
  return out;
}

function loadLocales(): Record<string, Bundle> {
  const bundles: Record<string, Bundle> = {};
  for (const file of readdirSync(I18N_DIR)) {
    if (!file.endsWith(".json")) continue;
    const locale = file.slice(0, -5);
    bundles[locale] = flatten(
      JSON.parse(readFileSync(path.join(I18N_DIR, file), "utf8")),
    );
  }
  return bundles;
}

/** i18next plural/ordinal suffixes plus this codebase's explicit numeric
 * variants (`offlineQueued_1`). Parity is asserted on the base key. */
function pluralBase(key: string): string {
  return key.replace(/_(zero|one|two|few|many|other|\d+)$/, "");
}

function baseKeySet(bundle: Bundle): Set<string> {
  return new Set(Object.keys(bundle).map(pluralBase));
}

function* walkSources(dir: string): Generator<string> {
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) {
      yield* walkSources(full);
    } else if (/\.(ts|tsx)$/.test(entry)) {
      yield full;
    }
  }
}

/** Collect the first argument region of a call, respecting quotes and
 * nesting, stopping at a depth-0 comma or the closing paren. */
function firstArgRegion(source: string, start: number): string {
  let depth = 0;
  let out = "";
  let i = start;
  while (i < source.length) {
    const c = source[i];
    if (c === '"' || c === "'" || c === "`") {
      out += c;
      i++;
      while (i < source.length) {
        out += source[i];
        if (source[i] === "\\") {
          i += 2;
          out += source[i - 1] ?? "";
          continue;
        }
        if (source[i] === c) {
          i++;
          break;
        }
        i++;
      }
      continue;
    }
    if (c === "(" || c === "[" || c === "{") depth++;
    if (c === ")" || c === "]" || c === "}") {
      if (depth === 0) return out;
      depth--;
    }
    if (c === "," && depth === 0) return out;
    out += c;
    i++;
  }
  return out;
}

const KEY_SHAPE = /^[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9_-]+)+$/;

function referencedKeys(): Map<string, string> {
  const refs = new Map<string, string>();
  for (const file of walkSources(SRC_DIR)) {
    if (file.startsWith(I18N_DIR)) continue;
    const source = readFileSync(file, "utf8");
    const callRe = /(?<![\w$])t\(/g;
    let match: RegExpExecArray | null;
    while ((match = callRe.exec(source))) {
      const region = firstArgRegion(source, match.index + 2);
      const literals = region.match(/"[^"\n]*"|'[^'\n]*'|`[^`\n]*`/g) ?? [];
      for (const literal of literals) {
        if (literal[0] === "`" && literal.includes("${")) continue; // dynamic
        const key = literal.slice(1, -1);
        if (KEY_SHAPE.test(key) && key.includes(".") && !refs.has(key)) {
          refs.set(key, path.relative(SRC_DIR, file));
        }
      }
    }
    const transRe = /i18nKey\s*=\s*["']([^"']+)["']/g;
    while ((match = transRe.exec(source))) {
      if (!refs.has(match[1])) refs.set(match[1], path.relative(SRC_DIR, file));
    }
  }
  return refs;
}

const bundles = loadLocales();

describe("i18n completeness", () => {
  it("ships exactly the expected locales", () => {
    expect(Object.keys(bundles).sort()).toEqual(EXPECTED_LOCALES);
  });

  const en = bundles.en;
  const enBases = baseKeySet(en);

  for (const locale of EXPECTED_LOCALES.filter((l) => l !== "en")) {
    it(`${locale} has the same key set as en`, () => {
      const bases = baseKeySet(bundles[locale]);
      const missing = [...enBases].filter((k) => !bases.has(k)).sort();
      const extra = [...bases].filter((k) => !enBases.has(k)).sort();
      expect(missing, `keys missing from ${locale}.json`).toEqual([]);
      expect(extra, `keys in ${locale}.json that en.json lacks`).toEqual([]);
    });
  }

  it("every statically referenced key exists in en.json", () => {
    const missing: string[] = [];
    for (const [key, file] of referencedKeys()) {
      const satisfied = key in en || enBases.has(pluralBase(key)) || enBases.has(key);
      if (!satisfied) missing.push(`${key} (referenced in ${file})`);
    }
    expect(missing.sort()).toEqual([]);
  });
});
