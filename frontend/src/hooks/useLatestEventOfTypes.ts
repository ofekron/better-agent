import { useMemo } from "react";
import type { WSEvent } from "src/types";

const EMPTY_EVENTS: readonly WSEvent[] = Object.freeze([]);

/**
 * Project the raw, per-token WS `events` buffer down to a
 * referentially-stable array holding only the single most-recent event
 * whose `type` is one of `types` (empty when none matched this turn).
 *
 * The raw buffer in `useWebSocket` grows on every streamed frame, so
 * passing it by identity into a React context makes that context churn
 * once per token. Consumers that only need discrete domain signals
 * (extension sidebars/panels reading `events[events.length - 1]`) then
 * re-render at token frequency — the source of the frozen/stale panels.
 *
 * The returned array's identity changes ONLY when the matched event
 * changes, so a context depending on it stays stable across the token
 * stream and mutates just when a relevant signal actually arrives. The
 * scan is a pure derivation (no render-time mutation), safe under
 * concurrent/StrictMode re-invocation; matching the last event of a few
 * types is negligible next to the React re-render storm it removes.
 *
 * `types` must be a stable (module-scope) array reference so the type
 * set is not rebuilt each render.
 */
export function useLatestEventOfTypes(
  events: readonly WSEvent[],
  types: readonly string[],
): readonly WSEvent[] {
  const typeSet = useMemo(() => new Set(types), [types]);
  const matched = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      if (typeSet.has(events[i].type)) return events[i];
    }
    return null;
  }, [events, typeSet]);
  return useMemo(() => (matched ? [matched] : EMPTY_EVENTS), [matched]);
}
