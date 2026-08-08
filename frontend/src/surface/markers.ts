// chat-panel.md's `renderModelMarkers`: "attach one marker to the last
// visible event of each contiguous provider/account/model/effort run."
// Pure function over whatever ordered node list is currently being
// rendered at a given scope (a turn's body, an explanation's members, a
// SubAgentTurn's members) — called identically at every nesting level, so
// nested-scope markers fall out of the SAME function with no special
// casing (ADR 0006 §1: "Every SubAgentTurn kind... carries its own full
// provider, model, and effort under the same last-event and per-boundary
// rule").

import type { NodeWire, RunWire } from "../adapter/wire";

function runKey(run: RunWire): string {
  return `${run.provider_id}::${run.account_name ?? ""}::${run.model}::${run.reasoning_effort ?? ""}`;
}

/** node_id -> the RunWire to show a marker for, at the LAST node of each
 * contiguous same-run stretch among nodes that carry a `run_ref`. Nodes
 * with no `run_ref` (structural/no-run content) don't break or extend a
 * run — they're simply skipped when looking for contiguity, since a
 * marker is about provider/model boundaries in actual execution, not
 * every node position. */
export function computeRunMarkers(
  nodes: readonly NodeWire[],
  runsById: ReadonlyMap<string, RunWire>,
): Map<string, RunWire> {
  const markers = new Map<string, RunWire>();
  let currentKey: string | null = null;
  let currentRun: RunWire | null = null;
  let lastNodeIdForRun: string | null = null;

  for (const node of nodes) {
    if (!node.run_ref) continue;
    const run = runsById.get(node.run_ref);
    if (!run) continue;
    const key = runKey(run);
    if (key !== currentKey) {
      if (currentRun && lastNodeIdForRun) markers.set(lastNodeIdForRun, currentRun);
      currentKey = key;
      currentRun = run;
    }
    lastNodeIdForRun = node.node_id;
  }
  if (currentRun && lastNodeIdForRun) markers.set(lastNodeIdForRun, currentRun);
  return markers;
}
