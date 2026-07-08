import { useMemo, useRef, useState, useId } from "react";
import { useTranslation } from "react-i18next";
import Icon from "./Icon";
import { SearchInput } from "./SearchInput";

export interface TagFilterOption {
  /** Matches an entry in `selectedTagIds` (manual tag id or `req:` key). */
  key: string;
  label: string;
  kind: "manual" | "product" | "feature";
  /** Tooltip for requirement tags (`kind: label`). */
  title?: string;
}

interface Props {
  options: TagFilterOption[];
  selectedTagIds: string[];
  onToggle: (key: string) => void;
}

/** Tag filter for the advanced search panel: instead of listing every
 *  available tag, the user searches tags in an autocomplete dropdown and
 *  only the selected tags remain rendered as removable chips. Manual and
 *  requirement tags share one `selectedTagIds` set, so both flow through
 *  the same option list and `onToggle`. */
export function TagFilterAutocomplete({ options, selectedTagIds, onToggle }: Props) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(-1);
  const inputRef = useRef<HTMLInputElement>(null);
  const listId = useId();

  const selectedKeys = useMemo(() => new Set(selectedTagIds), [selectedTagIds]);
  const selected = useMemo(
    () => options.filter((o) => selectedKeys.has(o.key)),
    [options, selectedKeys],
  );

  const suggestions = useMemo(() => {
    const q = query.trim().toLowerCase();
    return options.filter(
      (o) => !selectedKeys.has(o.key) && (q === "" || o.label.toLowerCase().includes(q)),
    );
  }, [options, selectedKeys, query]);

  const showDropdown = open && suggestions.length > 0;

  const pick = (key: string) => {
    onToggle(key);
    setQuery("");
    setHighlight(-1);
    inputRef.current?.focus();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      setHighlight((i) => (suggestions.length === 0 ? -1 : (i + 1) % suggestions.length));
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((i) => (suggestions.length === 0 ? -1 : i <= 0 ? suggestions.length - 1 : i - 1));
      return;
    }
    if (e.key === "Enter" && highlight >= 0 && highlight < suggestions.length) {
      e.preventDefault();
      pick(suggestions[highlight].key);
      return;
    }
    if (e.key === "Backspace" && query === "" && selected.length > 0) {
      // Empty field: Backspace removes the last selected chip.
      e.preventDefault();
      onToggle(selected[selected.length - 1].key);
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
      setHighlight(-1);
    }
  };

  const chipClass = (kind: TagFilterOption["kind"]) =>
    kind === "manual"
      ? "session-tag-toggle active session-tag-filter-chip"
      : `role-chip session-requirement-tag session-requirement-tag-${kind} session-requirement-tag-active session-tag-filter-chip`;

  return (
    <div className="session-tag-autocomplete">
      {selected.length > 0 && (
        <div className="session-tag-filter session-tag-filter-selected">
          {selected.map((o) => (
            <button
              key={o.key}
              type="button"
              className={chipClass(o.kind)}
              title={o.title}
              onClick={() => onToggle(o.key)}
              aria-label={t("session.removeTagFilter", { name: o.label })}
            >
              <span className="session-tag-filter-chip-label">{o.label}</span>
              <Icon name="x" size={11} className="session-tag-filter-chip-x" />
            </button>
          ))}
        </div>
      )}
      <div className="session-tag-autocomplete-field">
        <Icon name="search" size={12} className="session-tag-autocomplete-icon" />
        <SearchInput
          ref={inputRef}
          className="session-tag-autocomplete-input"
          value={query}
          placeholder={t("session.tagSearch")}
          role="combobox"
          aria-expanded={showDropdown}
          aria-controls={listId}
          aria-autocomplete="list"
          onFocus={() => setOpen(true)}
          onBlur={() => setOpen(false)}
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
            setHighlight(-1);
          }}
          onKeyDown={onKeyDown}
        />
      </div>
      {showDropdown && (
        <ul id={listId} className="session-tag-autocomplete-list" role="listbox" aria-label={t("session.tags")}>
          {suggestions.map((o, idx) => (
            <li key={o.key} role="presentation">
              <button
                type="button"
                role="option"
                aria-selected={idx === highlight}
                title={o.title}
                className={`session-tag-autocomplete-item${idx === highlight ? " highlighted" : ""}${o.kind !== "manual" ? ` session-tag-autocomplete-item-${o.kind}` : ""}`}
                // preventDefault keeps focus on the input so the pick fires
                // before the blur closes the dropdown.
                onMouseDown={(e) => {
                  e.preventDefault();
                  pick(o.key);
                }}
                onMouseEnter={() => setHighlight(idx)}
              >
                {o.kind !== "manual" && <span className={`session-tag-autocomplete-kind session-tag-autocomplete-kind-${o.kind}`} />}
                <span className="session-tag-autocomplete-item-label">{o.label}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {open && query.trim() !== "" && suggestions.length === 0 && (
        <div className="session-tag-autocomplete-empty">{t("session.noTagMatch")}</div>
      )}
    </div>
  );
}
