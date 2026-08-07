// Non-component node-kind predicates shared across nodes/rows.tsx and
// nodes/ItemRow.tsx.

import type { NodeWire } from "../../adapter/wire";

export function isContainerKind(kind: NodeWire["kind"]): boolean {
  return (
    kind === "explanation" ||
    kind === "native_subagent_turn" ||
    kind === "worker_turn" ||
    kind === "sub_session_turn" ||
    kind === "session_turn"
  );
}
