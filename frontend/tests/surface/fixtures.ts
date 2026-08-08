// Shared NodeWire/frame builders for the Phase I stage 1 native-surface
// grammar-conformance suite (frontend/tests/surface/*). Mirrors the
// fixture style already established in ../useSurfaceSession.test.ts —
// kept local to this suite (not merged into that file) to avoid touching
// existing coverage.

import type {
  ChildManifestWire,
  CompactSessionSnapshotWire,
  NodeKindWire,
  NodeWire,
  RunWire,
  TurnLifecycleFrame,
  TurnPhaseWire,
} from "../../src/adapter/wire";

export const SESSION = "s1";

let seq = 0;
export function resetSeq() {
  seq = 0;
}

export function node(partial: Partial<NodeWire> & { kind: NodeKindWire; turn_id: string; node_id: string }): NodeWire {
  return {
    cv: 1,
    parent_id: null,
    surface_id: SESSION,
    ts: 100 + seq,
    seq: seq++,
    status: null,
    payload: null,
    run_ref: null,
    sidecar_ref: null,
    target_ref: null,
    child_manifest: null,
    ...partial,
  };
}

export function turnNode(
  turnId: string,
  manifest: ChildManifestWire = { renderable_child_count: 0, has_children: false },
  surfaceId: string = SESSION,
  /** Team-vs-native turn chip source (backend `nodes.py`'s `Node.
   * orchestration_mode`) — trailing optional param, same append-without-
   * breaking-existing-callers convention `promptNode`'s `intentId` used. */
  orchestrationMode: string | null = null,
): NodeWire {
  return node({
    node_id: `turn:${turnId}`, turn_id: turnId, kind: "turn", child_manifest: manifest, surface_id: surfaceId,
    orchestration_mode: orchestrationMode,
  });
}

/** `intentId` (trailing, optional, default null matching every pre-existing
 * caller) stamps `TypedPromptPayloadWire.intent_id` — needed to build a
 * confirmed node that `SurfaceStore.reconcileProvisionalSend` (state.ts)
 * will match against a `sendPrompt`-scaffolded provisional turn, without
 * depending on the backend's own intent_id-propagation landing (per this
 * round's ledger note: build against the field, tests stamp it directly). */
export function promptNode(
  turnId: string,
  text: string,
  surfaceId: string = SESSION,
  intentId: string | null = null,
): NodeWire {
  return node({
    node_id: `${turnId}:prompt`,
    turn_id: turnId,
    kind: "typed_prompt",
    status: "complete",
    surface_id: surfaceId,
    payload: { text, attachments: [], send_mode: "queue", origin: "user", source_session_ref: null, sent_text: null, intent_id: intentId },
  });
}

export function resultNode(turnId: string, text: string): NodeWire {
  return node({
    node_id: `${turnId}:result`,
    turn_id: turnId,
    kind: "result",
    payload: { result_kind: "provider", text, is_error: false },
  });
}

export function assistantTextNode(turnId: string, nodeId: string, text: string, parentId: string, status: NodeWire["status"] = "complete"): NodeWire {
  return node({ node_id: nodeId, turn_id: turnId, kind: "assistant_text", parent_id: parentId, status, payload: { text } });
}

export function toolInteractionNode(turnId: string, nodeId: string, toolName: string, parentId: string, status: NodeWire["status"] = "complete"): NodeWire {
  return node({
    node_id: nodeId,
    turn_id: turnId,
    kind: "tool_interaction",
    parent_id: parentId,
    status,
    payload: { tool_name: toolName, args: {}, result: null, interaction_ref: null, ui_kind: null, derived_view: null },
  });
}

export function explanationNode(turnId: string, nodeId: string, parentId: string, manifest: ChildManifestWire): NodeWire {
  return node({ node_id: nodeId, turn_id: turnId, kind: "explanation", parent_id: parentId, child_manifest: manifest });
}

/** `model_change` node — attached to the turn it affects via `compactTurn`'s
 *  `runtimeChange` param (TurnView.tsx renders it as a boundary BEFORE that
 *  turn), never as a standalone content child. */
export function modelChangeNode(
  turnId: string,
  fromRunRef: string | null,
  toRunRef: string,
  source: "user" | "provider" = "provider",
): NodeWire {
  return node({
    node_id: `${turnId}:model-change`,
    turn_id: turnId,
    kind: "model_change",
    payload: { from_run_ref: fromRunRef, to_run_ref: toRunRef, source },
  });
}

export function nativeSubagentTurnNode(turnId: string, nodeId: string, parentId: string, manifest: ChildManifestWire): NodeWire {
  return node({ node_id: nodeId, turn_id: turnId, kind: "native_subagent_turn", parent_id: parentId, child_manifest: manifest });
}

export function failureNode(
  turnId: string,
  nodeId: string,
  parentId: string,
  overrides: Partial<import("../../src/adapter/wire").FailurePayloadWire> = {},
): NodeWire {
  return node({
    node_id: nodeId,
    turn_id: turnId,
    kind: "failure",
    parent_id: parentId,
    payload: {
      code: "provider_error",
      text: "Something went wrong",
      data: null,
      severity: "error",
      retryable: false,
      resolution: "none",
      ...overrides,
    },
  });
}

export function runWire(runRef: string, overrides: Partial<RunWire> = {}): RunWire {
  return {
    run_ref: runRef,
    provider_id: "anthropic",
    account_name: null,
    model: "claude",
    reasoning_effort: null,
    runner: "cli",
    ...overrides,
  };
}

export function compactTurn(
  turn: NodeWire,
  prompt: NodeWire | null,
  results: NodeWire[],
  runtimeChange: NodeWire | null = null,
) {
  return {
    turn,
    prompt,
    results,
    manifest: turn.child_manifest ?? { renderable_child_count: 0, has_children: false },
    runtime_change: runtimeChange,
  };
}

export function snapshotEnvelope(
  turns: ReturnType<typeof compactTurn>[],
  liveTurnNodes: NodeWire[] = [],
  opts: { incarnation?: string; renderRev?: number; olderCursor?: string | null; runs?: RunWire[] } = {},
) {
  const body: CompactSessionSnapshotWire = {
    session_id: SESSION,
    surface_id: SESSION,
    instruction_widget: null,
    turns,
    live_turn_nodes: liveTurnNodes,
    runs: opts.runs ?? [],
    older_cursor: opts.olderCursor ?? null,
  };
  return {
    ...body,
    kind: "ok" as const,
    snapshot_identity: { incarnation: opts.incarnation ?? "inc-1", render_rev: opts.renderRev ?? 0, hist_rev: 0 },
  };
}

/** `turn_lifecycle` live frame — the sole source of `TurnEntry.phase`
 *  (state.ts's `isLivePhase`), used by tests/runs.test.ts's native
 *  live-run-indicator suite to drive a seeded turn in/out of a live
 *  phase via `h.emitSurface`. */
export function turnLifecycleFrame(
  turnId: string,
  phase: TurnPhaseWire,
  overrides: Partial<TurnLifecycleFrame> = {},
): TurnLifecycleFrame {
  return {
    type: "turn_lifecycle",
    cv: 1,
    surface_id: SESSION,
    snapshot: { incarnation: "mock", render_rev: 1, hist_rev: 0 },
    turn_id: turnId,
    phase,
    reason: null,
    usage: null,
    ...overrides,
  };
}

export function jsonResponse(body: unknown) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(body) } as Response);
}

/** A `typed_prompt` node with full payload control (origin/
 * source_session_ref/sent_text/attachments) — `promptNode` above only ever
 * builds the plain `origin: "user"` case, insufficient for origin-label
 * parity coverage (round 2's TypedPromptView gap). Appended rather than
 * extending `promptNode`'s signature, per this file's append-only-shared-
 * fixture convention. */
export function typedPromptNode(
  turnId: string,
  payload: Partial<import("../../src/adapter/wire").TypedPromptPayloadWire> & { text: string },
  surfaceId: string = SESSION,
): NodeWire {
  return node({
    node_id: `${turnId}:prompt`,
    turn_id: turnId,
    kind: "typed_prompt",
    status: payload.origin === "queued" ? "queued" : "complete",
    surface_id: surfaceId,
    payload: {
      attachments: [],
      send_mode: "queue",
      origin: "user",
      source_session_ref: null,
      sent_text: null,
      intent_id: null,
      ...payload,
    },
  });
}

/** Shared override shape for the SubAgentTurn-family builders below.
 * `label`/`usage` mirror backend's now-real `SubAgentTurnPayload.label`
 * and `Node.usage` (phase2-inventory.md, "worker_turn / sub_session_turn
 * BACKEND producer" round) — `undefined` (the default) leaves
 * `payload`/`usage` at `node()`'s own defaults (`null`/omitted), NOT a
 * fabricated value. */
interface SubAgentTurnOverrides {
  runRef?: string;
  targetRef?: import("../../src/adapter/wire").TargetRefWire;
  label?: string;
  usage?: import("../../src/adapter/wire").UsageWire;
  /** Mirrors backend's `SubAgentTurnPayload.created` (the legacy
   * `*_created` panel variant — target session already existed, nothing
   * ran). `undefined` (the default) leaves `payload` at `node()`'s own
   * default (`null`) when `label` is also unset, matching every
   * pre-existing call site unchanged. */
  created?: boolean;
}

/** `worker_turn` node — one of the SubAgentTurn family's four sourcing
 * kinds (nodes.py's `SUBAGENT_TURN_KINDS`), now backend-produced for the
 * ask/delegate_task flow (phase2-inventory.md) — built here so the
 * frontend render contract has fixture-driven coverage independent of a
 * live backend. `run_ref`/`target_ref`/`label`/`usage` overrides let a
 * caller cover the RunMarker/target-link/label/usage-overlay affordances
 * without a second builder. */
export function workerTurnNode(turnId: string, nodeId: string, parentId: string, manifest: ChildManifestWire, overrides: SubAgentTurnOverrides = {}): NodeWire {
  return node({
    node_id: nodeId,
    turn_id: turnId,
    kind: "worker_turn",
    parent_id: parentId,
    child_manifest: manifest,
    run_ref: overrides.runRef ?? null,
    target_ref: overrides.targetRef ?? null,
    payload:
      overrides.label !== undefined || overrides.created !== undefined
        ? { label: overrides.label ?? null, created: overrides.created ?? false }
        : null,
    usage: overrides.usage ?? null,
  });
}

/** `sub_session_turn` node — see `workerTurnNode`'s docstring, same
 * disposition (SubAgentTurn family). */
export function subSessionTurnNode(turnId: string, nodeId: string, parentId: string, manifest: ChildManifestWire, overrides: SubAgentTurnOverrides = {}): NodeWire {
  return node({
    node_id: nodeId,
    turn_id: turnId,
    kind: "sub_session_turn",
    parent_id: parentId,
    child_manifest: manifest,
    run_ref: overrides.runRef ?? null,
    target_ref: overrides.targetRef ?? null,
    payload:
      overrides.label !== undefined || overrides.created !== undefined
        ? { label: overrides.label ?? null, created: overrides.created ?? false }
        : null,
    usage: overrides.usage ?? null,
  });
}
