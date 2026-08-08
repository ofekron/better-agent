import { useEffect, useState } from "react";
import { runSummaryRegistry } from "../lib/runSummaryRegistry";
import type { RunSummaryWire } from "../adapter/wire";

/** Subscribes to every live run-summary change for `sessionId`, forcing a
 * re-render on each one — the one piece of subscribe/hydrate wiring both
 * `useRunSummary`/`useRunSummaryByTurn` below share, since they differ
 * only in which registry LOOKUP they read afterward (by `run_id` vs
 * `turn_id`), not in how they stay live. */
function useSubscribedSession(sessionId: string | undefined): void {
  const [, forceRender] = useState(0);
  useEffect(() => {
    if (!sessionId) return;
    return runSummaryRegistry.subscribeSession(sessionId, () => forceRender((n) => n + 1));
  }, [sessionId]);
}

/** Live v2 `RunSummary` (ADR 0009) for one `run_id`, scoped to
 * `sessionId` for hydration/subscription — cold-hydrates that session's
 * run list via `GET /runs?session_id=` on first mount and stays live via
 * the `runs` feed's `run_summary_upsert` afterward (`runSummaryRegistry`
 * owns the single shared connection).
 *
 * `undefined` whenever this `run_id` has no known v2 record yet — e.g.
 * hydration still in flight, or this run predates the registry's live
 * feed. Callers must treat that as "unknown", never as "not stalled" /
 * "not running" (same honesty-first rule the backend adapter itself
 * documents for its own gaps) — `RunBadge.tsx` only ever uses this as an
 * ADDITIONAL signal layered on top of the legacy `RunInfo` prop it
 * already has, never as the sole source of truth. */
export function useRunSummary(
  sessionId: string | undefined,
  runId: string | undefined,
): RunSummaryWire | undefined {
  useSubscribedSession(sessionId);
  if (!runId) return undefined;
  return runSummaryRegistry.getRun(runId);
}

/** Live v2 `RunSummary`, correlated by `turn_id` instead of `run_id` — for
 * a content-tree caller (`surface/TurnView.tsx`, which has a `NodeWire.
 * turn_id` at hand but never a `run_id`; `NodeWire.run_ref` is never
 * populated by the current backend adapter). Same hydrate-then-live-
 * subscribe contract as `useRunSummary` above, just a different lookup
 * key into the SAME shared registry. */
export function useRunSummaryByTurn(
  sessionId: string | undefined,
  turnId: string | undefined,
): RunSummaryWire | undefined {
  useSubscribedSession(sessionId);
  if (!sessionId || !turnId) return undefined;
  return runSummaryRegistry.getRunByTurnId(sessionId, turnId);
}
