// Terminal "stopped" phase indicator — reuses legacy MessageBubble.tsx's
// `StoppedIndicator` visual language 1:1 (`.stopped-indicator` CSS,
// styles/globals.css, no new styling needed). Native carries one phase
// enum per turn (`TurnEntry.phase`, state.ts) instead of legacy's
// per-message `stopped_at`/`interrupted_by_msg_id` fields, and the
// `turn_lifecycle` wire frame (wire.ts's `TurnLifecycleFrame`) carries no
// stop timestamp at all — unlike legacy this renders no clock time, only
// the interrupted-vs-stopped distinction derived from `TerminalReasonWire`
// (`user_stopped` = the user interrupted it, matching legacy's
// `interrupted` flag; anything else falls back to the generic "Stopped"
// label legacy used for a non-interrupt stop).

import type { TerminalReasonWire } from "../../adapter/wire";

export function StoppedIndicator({ reason }: { reason: TerminalReasonWire | null }) {
  const interrupted = reason === "user_stopped";
  return (
    <div className="stopped-indicator" data-testid="surface-stopped-indicator">
      {interrupted ? "Interrupted" : "Stopped"}
    </div>
  );
}
