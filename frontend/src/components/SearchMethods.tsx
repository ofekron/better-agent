import { useTranslation } from "react-i18next";
import type { SearchMethod } from "../types";

const ORDER: SearchMethod[] = ["path", "name", "symbols"];
const LABEL_KEY: Record<SearchMethod, string> = {
  path: "picker.methodPath",
  name: "picker.methodName",
  symbols: "picker.methodSymbols",
};

/**
 * Multi-select search-method chips shared by the file and dir pickers.
 *
 * INVARIANT: at least one method stays active — clicking the only active
 * chip is a no-op (an empty selection matches nothing server-side).
 */
export function SearchMethods({
  available,
  value,
  onChange,
}: {
  available: SearchMethod[];
  value: SearchMethod[];
  onChange: (v: SearchMethod[]) => void;
}) {
  const { t } = useTranslation();

  const toggle = (m: SearchMethod) => {
    const active = value.includes(m);
    if (active && value.length === 1) return;
    const next = active ? value.filter((x) => x !== m) : [...value, m];
    onChange(ORDER.filter((x) => next.includes(x)));
  };

  return (
    <div className="picker-methods">
      {ORDER.filter((m) => available.includes(m)).map((m) => (
        <button
          key={m}
          type="button"
          className={`picker-method-chip ${value.includes(m) ? "active" : ""}`}
          onClick={() => toggle(m)}
        >
          {t(LABEL_KEY[m])}
        </button>
      ))}
    </div>
  );
}
