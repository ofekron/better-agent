// Per-turn token-usage summary — native equivalent of legacy
// MessageBubble.tsx's `CompleteEvent` "In: X / Out: Y / Cache: Z" line
// (`.event-complete-success`/`.token-sep` CSS, both pre-existing, no new
// rules needed). `TurnEntry.usage` (surface/state.ts) is populated from
// `turn_lifecycle` frames but, until this component existed, was never
// read by anything under src/surface/** — captured data, never rendered.
//
// `UsageWire` (adapter/wire.ts) now carries `cache_creation_input_tokens`/
// `cache_read_input_tokens` (backend/surface_contract/nodes.py's `Usage`,
// via `usage_from_raw`) — the "Cache: Z" segment mirrors legacy's exact
// source field (`cache_read_input_tokens` only; legacy never read/showed
// `cache_creation_input_tokens`), shown only when > 0, same as legacy.
//
// Native delta from legacy still open: legacy rendered one summary per
// "complete" WSEvent, including one per completed SUBAGENT run; the
// native wire's `usage` field lives ONLY on `TurnLifecycleFrame`
// (turn-level) — no SubAgentTurn/worker_turn node kind carries its own
// usage payload (surface_contract/nodes.py has no such field), so a
// per-subagent breakdown is not buildable from today's wire. Backend-side
// data gap, documented rather than faked.

import type { UsageWire } from "../../adapter/wire";

/** Same K/M abbreviation as legacy's `fmt()` (MessageBubble.tsx) —
 * re-derived locally rather than imported, since that formatter lives in
 * a file slated for deletion and had no other native consumer to share it
 * with yet (see this module's own docstring). */
function fmt(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
  return n.toString();
}

export function UsageSummary({ usage }: { usage: UsageWire | null }) {
  if (!usage) return null;
  const inTok = usage.input_tokens ?? 0;
  const outTok = usage.output_tokens ?? 0;
  if (inTok + outTok === 0) return null;
  const cacheRead = usage.cache_read_input_tokens ?? 0;
  return (
    <div className="event-complete-success" data-testid="surface-usage-summary">
      <span>In: {fmt(inTok)}</span>
      <span className="token-sep">/</span>
      <span>Out: {fmt(outTok)}</span>
      {cacheRead > 0 && (
        <>
          <span className="token-sep">/</span>
          <span>Cache: {fmt(cacheRead)}</span>
        </>
      )}
    </div>
  );
}
