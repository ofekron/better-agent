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

export function turnNode(turnId: string, manifest: ChildManifestWire = { renderable_child_count: 0, has_children: false }): NodeWire {
  return node({ node_id: `turn:${turnId}`, turn_id: turnId, kind: "turn", child_manifest: manifest });
}

export function promptNode(turnId: string, text: string): NodeWire {
  return node({
    node_id: `${turnId}:prompt`,
    turn_id: turnId,
    kind: "typed_prompt",
    status: "complete",
    payload: { text, attachments: [], send_mode: "queue", origin: "user", source_session_ref: null, sent_text: null, intent_id: null },
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
    payload: { tool_name: toolName, args: {}, result: null, approval_ref: null, ui_kind: null, derived_view: null },
  });
}

export function explanationNode(turnId: string, nodeId: string, parentId: string, manifest: ChildManifestWire): NodeWire {
  return node({ node_id: nodeId, turn_id: turnId, kind: "explanation", parent_id: parentId, child_manifest: manifest });
}

export function nativeSubagentTurnNode(turnId: string, nodeId: string, parentId: string, manifest: ChildManifestWire): NodeWire {
  return node({ node_id: nodeId, turn_id: turnId, kind: "native_subagent_turn", parent_id: parentId, child_manifest: manifest });
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

export function compactTurn(turn: NodeWire, prompt: NodeWire | null, results: NodeWire[]) {
  return {
    turn,
    prompt,
    results,
    manifest: turn.child_manifest ?? { renderable_child_count: 0, has_children: false },
    runtime_change: null,
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

export function jsonResponse(body: unknown) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(body) } as Response);
}
