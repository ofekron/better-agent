import { useTranslation } from "react-i18next";
import Icon from "./Icon";
import { SESSION_STATUS_KEYS, type SessionStatusKey } from "../lib/sessionRegistry";
import type { SessionStatusFilters } from "../lib/sessionStatusFilters";

type FilterDisclosureProps = {
  id: string;
  label: string;
  /** Number of active selections, shown on the collapsed header. */
  activeCount?: number;
  variant?: "section" | "group";
  hint?: string;
  isExpanded: (id: string) => boolean;
  onToggle: (id: string) => void;
  children: React.ReactNode;
};

/** A titled, collapsible filter block. Used for both the panel's sections and
 * every individual filter inside them so the panel costs one line per filter
 * until the user opens one. */
export function FilterDisclosure({
  id,
  label,
  activeCount = 0,
  variant = "group",
  hint,
  isExpanded,
  onToggle,
  children,
}: FilterDisclosureProps) {
  const expanded = isExpanded(id);
  return (
    <div
      className={
        variant === "section"
          ? "session-filter-section session-filter-disclosure"
          : "session-filter-group session-filter-disclosure"
      }
      data-expanded={expanded}
    >
      <button
        type="button"
        className={`session-filter-disclosure-head ${
          variant === "section" ? "session-filter-section-title" : "session-filter-label"
        }`}
        aria-expanded={expanded}
        title={hint ?? label}
        onClick={() => onToggle(id)}
      >
        <Icon
          name={expanded ? "chevron-down" : "chevron-right"}
          size={10}
          className="session-filter-disclosure-chevron"
        />
        <span className="session-filter-disclosure-label">{label}</span>
        {activeCount > 0 && <span className="session-group-count">{activeCount}</span>}
      </button>
      {expanded && <div className="session-filter-disclosure-body">{children}</div>}
    </div>
  );
}

type StatusFilterChipsProps = {
  value: SessionStatusFilters;
  onCycle: (key: SessionStatusKey) => void;
};

/** One chip per status bucket. Click shows only that bucket, click again hides
 * it, click again clears — so "show these" and "hide those" share one control. */
export function StatusFilterChips({ value, onCycle }: StatusFilterChipsProps) {
  const { t } = useTranslation();
  return (
    <div className="session-tag-filter session-status-filter">
      {SESSION_STATUS_KEYS.map((key) => {
        const mode = value[key];
        const stateLabel = t(
          mode === "include"
            ? "session.statusStateIncluded"
            : mode === "exclude"
              ? "session.statusStateExcluded"
              : "session.statusStateNeutral",
        );
        return (
          <button
            key={key}
            type="button"
            className={`session-tag-toggle session-status-toggle ${mode ?? "neutral"} ${
              mode === "include" ? "active" : ""
            }`}
            aria-pressed={mode === "include"}
            title={`${t(`session.status.${key}`)} — ${stateLabel}`}
            onClick={() => onCycle(key)}
          >
            <Icon
              name={mode === "exclude" ? "x" : mode === "include" ? "check" : "circle"}
              size={10}
              className="session-status-toggle-mark"
            />
            <span>{t(`session.status.${key}`)}</span>
          </button>
        );
      })}
    </div>
  );
}
