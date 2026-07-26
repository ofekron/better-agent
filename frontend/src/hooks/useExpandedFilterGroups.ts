import { useCallback, useMemo, useState } from "react";

/** Section shells open by default; the filters inside them start collapsed. */
export const DEFAULT_EXPANDED_FILTER_GROUPS = ["filters.global", "filters.project"] as const;

const EXPANDED_FILTER_GROUPS_LS_KEY = "better-agent-session-filter-groups-expanded";

function readExpandedFilterGroups(): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(EXPANDED_FILTER_GROUPS_LS_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed).filter(([, open]) => typeof open === "boolean"),
    ) as Record<string, boolean>;
  } catch {
    return {};
  }
}

/** Open/closed state per filter disclosure. Pure UI preference (like panel
 * widths), so localStorage is its home. Individual filters start collapsed so
 * the panel costs one line each — active ones still announce themselves via
 * the header count — while `defaultExpandedIds` (the section shells) start
 * open so every filter stays one click away. An explicit user toggle is
 * remembered per id, so collapsing a default-open section sticks. */
export function useExpandedFilterGroups(defaultExpandedIds: readonly string[] = []): {
  isExpanded: (id: string) => boolean;
  toggle: (id: string) => void;
} {
  const [state, setState] = useState<Record<string, boolean>>(readExpandedFilterGroups);
  const defaultsKey = defaultExpandedIds.join(",");
  const defaults = useMemo(() => new Set(defaultsKey ? defaultsKey.split(",") : []), [defaultsKey]);
  const isExpanded = useCallback(
    (id: string) => state[id] ?? defaults.has(id),
    [state, defaults],
  );
  const toggle = useCallback(
    (id: string) => {
      setState((prev) => {
        const next = { ...prev, [id]: !(prev[id] ?? defaults.has(id)) };
        try {
          localStorage.setItem(EXPANDED_FILTER_GROUPS_LS_KEY, JSON.stringify(next));
        } catch {
          // localStorage unavailable — the open set just won't persist.
        }
        return next;
      });
    },
    [defaults],
  );
  return { isExpanded, toggle };
}
