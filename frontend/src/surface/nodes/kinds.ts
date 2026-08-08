// Non-component node-kind predicates shared across nodes/rows.tsx and
// nodes/ItemRow.tsx.

import type { NodeWire } from "../../adapter/wire";

export function isContainerKind(kind: NodeWire["kind"]): boolean {
  return (
    kind === "explanation" ||
    kind === "native_subagent_turn" ||
    kind === "worker_turn" ||
    kind === "sub_session_turn" ||
    kind === "session_turn" ||
    // `compaction`'s pre-compaction replayed content, when the backend
    // populates it, hangs off the SAME generic child_manifest/children(id)
    // mechanism every other container uses (see nodes/Continuation.tsx's
    // CompactionView docstring) — not a live-streaming container, but
    // still needs `store`/`runsById` threaded through ItemRow to expand.
    kind === "compaction"
  );
}

/** `lifecycle_notice` | `diagnostic` — the two kinds backend's
 * `derive.py`'s `_NON_RENDERABLE_KINDS` excludes from
 * `renderable_child_count` specifically because they "render at their
 * occurrence rather than as expandable children" (derive.py's own
 * comment: "in-place notices... never a ChatItem"; "A lifecycle-only turn
 * has zero renderable children and no three-dot process control") — they
 * must stay visible even when their parent turn is fully collapsed, never
 * folded behind an ellipsis (round 2 gap: TurnView.tsx's collapsed branch
 * previously rendered nothing from the body at all). `failure` is
 * deliberately NOT included here — backend's own `_NON_RENDERABLE_KINDS`
 * set does not exempt it, so it stays ordinary ellipsis-gated content,
 * consistent with `renderable_child_count` counting it. */
export function isAlwaysInPlaceKind(kind: NodeWire["kind"]): boolean {
  return kind === "lifecycle_notice" || kind === "diagnostic";
}

/** Picks the trailing preview candidate from a container's cached body —
 * the last item that is neither an always-in-place kind (rendered
 * separately, unconditionally elsewhere), a container kind, nor `failure`.
 *
 * Container kinds are excluded because previewing one recursively via
 * `NodeView` would mount its OWN stateful collapse/fetch machinery
 * (`ExplanationView`'s `useChildren(..., true)` in particular is
 * unconditional) — defeating "collapsed rendering never fetches" and
 * risking a duplicate-testid/duplicate-disclosure-button render.
 *
 * `failure` is excluded by the SAME backend-confirmed design
 * `tests/surface/collapsedStatusPills.test.tsx` documents: it counts
 * toward `renderable_child_count` (ordinary ellipsis-gated content, unlike
 * lifecycle_notice/diagnostic) but must stay fully hidden behind the
 * ellipsis until explicitly expanded — never shown, not even as a preview.
 *
 * Matches legacy's own last-item preview, which only ever rendered a
 * simple leaf shape, never a recursively-expanded nested block or an
 * error a user hasn't chosen to look at yet. */
export function lastPreviewCandidate(items: readonly NodeWire[]): NodeWire | null {
  const candidates = items.filter(
    (n) => !isAlwaysInPlaceKind(n.kind) && !isContainerKind(n.kind) && n.kind !== "failure",
  );
  return candidates.length > 0 ? candidates[candidates.length - 1] : null;
}
