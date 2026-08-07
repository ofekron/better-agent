// Property test: useSurfaceSession's incremental live-frame path (turnNodeTable
// + mapTurnToAssistantMessage's eventsCache/subagentChildren) must produce
// EXACTLY the same ChatMessage as the from-scratch pure mapper, at every step
// of a random sequence of node_upsert / text_delta / node_status frames — not
// just at the end. Also covers native_subagent_turn structural nodes and
// their lazy (async-fetch-simulated) children arrival, mirroring
// useSurfaceSession.ts's maybeFetchSubagentChildren resolution callback
// (subagentChildren.set + eventsCache.delete(subagentNodeId)). A handful of
// deterministic seeds gives reproducible, CI-stable coverage of the keyed-
// patching machinery without depending on a property-testing library.

import { describe, expect, it } from "vitest";
import {
  applyNodeStatusToNode,
  applyTextDeltaToNode,
  mapTurnToAssistantMessage,
  parseSubagentAnchorToolUseId,
  type EventsCache,
} from "../src/adapter/mapToRenderModel";
import {
  createTurnNodeTable,
  upsertTurnNode,
  getTurnNode,
  type TurnNodeTable,
} from "../src/adapter/turnNodeTable";
import type { CompactTurnWire, NodeWire, RunWire } from "../src/adapter/wire";

const SURFACE = "s1";
const TURN = "prompt-uuid-1";

function turn(): CompactTurnWire {
  return {
    turn: {
      cv: 1, node_id: `turn:${TURN}`, parent_id: null, turn_id: TURN, surface_id: SURFACE,
      kind: "turn", ts: 500, seq: 0, status: "streaming", payload: null, run_ref: null,
      sidecar_ref: null, target_ref: null, child_manifest: null,
    },
    prompt: null,
    results: [],
    manifest: { renderable_child_count: 0, has_children: false },
    runtime_change: null,
  };
}

function mulberry32(seed: number): () => number {
  let a = seed;
  return function rng() {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function pick<T>(rng: () => number, arr: readonly T[]): T {
  return arr[Math.floor(rng() * arr.length)];
}

const RUNS: RunWire[] = [
  { run_ref: "run-a", provider_id: "claude", account_name: null, model: "sonnet-4-6", reasoning_effort: "high", runner: "native" },
  { run_ref: "run-b", provider_id: "openai", account_name: null, model: "gpt-5", reasoning_effort: "medium", runner: "cli" },
];
const RUNS_BY_ID = new Map(RUNS.map((r) => [r.run_ref, r]));

const NODE_ID_POOL = Array.from({ length: 24 }, (_, i) => `n${i}`);
// Pre-formatted `subagent:{anchor_node_id}` ids — parseSubagentAnchorToolUseId
// (mapToRenderModel.ts) requires this exact shape to resolve an anchor at
// all, so these are a dedicated pool rather than folded into NODE_ID_POOL.
const SUBAGENT_NODE_ID_POOL = Array.from({ length: 6 }, (_, i) => `subagent:tool:sa${i}`);
const TEXT_KINDS = ["assistant_text", "thinking"] as const;
const STATUSES: NodeWire["status"][] = ["queued", "streaming", "partial", "complete", "failed", "stopped"];

function subagentTurnNode(id: string, ts: number, seq: number, childCount: number): NodeWire {
  return {
    cv: 1, node_id: id, parent_id: null, turn_id: TURN, surface_id: SURFACE,
    kind: "native_subagent_turn", ts, seq, status: "complete", payload: null, run_ref: null,
    sidecar_ref: null, target_ref: null,
    child_manifest: { renderable_child_count: childCount, has_children: childCount > 0 },
  };
}

/** Fabricates a small, self-contained child NodeWire[] for a
 * native_subagent_turn's lazily-fetched body — distinct node_ids per call
 * (never collides with the main node pool or an earlier resolution). */
function fabricateSubagentChildren(seed: string, count: number): NodeWire[] {
  return Array.from({ length: count }, (_, i) => ({
    cv: 1, node_id: `${seed}-child-${i}`, parent_id: null, turn_id: `${TURN}-sub`, surface_id: SURFACE,
    kind: "assistant_text" as const, ts: 1, seq: i, status: "complete" as const,
    payload: { text: `subagent output ${seed}-${i}` }, run_ref: null,
    sidecar_ref: null, target_ref: null, child_manifest: null,
  }));
}

function freshPayload(kind: string, rng: () => number, seq: number): NodeWire["payload"] {
  switch (kind) {
    case "assistant_text":
      return { text: `text-${seq}` };
    case "thinking":
      return { text: `thought-${seq}`, redacted: false };
    case "tool_interaction":
      return {
        tool_name: "Bash", args: { cmd: `cmd-${seq}` },
        result: rng() < 0.3 ? { output: `out-${seq}` } : null,
        approval_ref: null, ui_kind: null, derived_view: null,
      };
    case "diagnostic":
      return { severity: "info", code: "other", text: `diag-${seq}`, data: null };
    case "failure":
      return { code: "provider_error", text: `fail-${seq}`, data: null, severity: "error", retryable: true, resolution: "retry" };
    case "continuation_session":
      return { execution_ref: `exec-${seq}`, chain_depth: seq % 5, summary: null };
    case "worker_interaction":
      return rng() < 0.5
        ? { fact_kind: "worker_start", fact: { delegation_id: `d${seq % 4}`, worker_session_id: null, worker_description: `w${seq}`, is_new: true, instructions_preview: "" } }
        : { fact_kind: "worker_complete", fact: { delegation_id: `d${seq % 4}`, success: true } };
    default:
      throw new Error(`unhandled kind ${kind}`);
  }
}

const KINDS = ["assistant_text", "thinking", "tool_interaction", "diagnostic", "failure", "continuation_session", "worker_interaction"] as const;

interface Step {
  action:
    | "new_node"
    | "update_existing"
    | "text_delta"
    | "node_status"
    | "new_subagent_node"
    | "resolve_subagent_children";
}

/** Runs one deterministic random scenario, asserting incremental == from-scratch after EVERY step. */
function runScenario(seed: number, stepCount: number): void {
  const rng = mulberry32(seed);
  const table: TurnNodeTable = createTurnNodeTable();
  const eventsCache: EventsCache = new Map();
  const refNodes = new Map<string, NodeWire>();
  const tsSeqById = new Map<string, { ts: number; seq: number }>();
  let nextTs = 1000;
  const usedIds: string[] = [];
  // Shared by reference between the incremental and from-scratch calls
  // below — mirrors how BOTH read off the SAME useSurfaceSession
  // TurnState.subagentChildren in production. What's under test is
  // whether `eventsCache` was correctly invalidated when this map
  // changes, not whether the two calls "see" different data (they never
  // do — it's the same object).
  const subagentChildren: Map<string, NodeWire[]> = new Map();
  const usedSubagentIds: string[] = [];

  for (let step = 0; step < stepCount; step++) {
    const action: Step["action"] =
      usedIds.length === 0
        ? "new_node"
        : pick(rng, [
            "new_node", "new_node", "update_existing", "text_delta", "node_status",
            "new_subagent_node", "resolve_subagent_children", "resolve_subagent_children",
          ] as const);

    if (action === "new_node" && usedIds.length < NODE_ID_POOL.length) {
      const id = NODE_ID_POOL[usedIds.length];
      usedIds.push(id);
      const kind = pick(rng, KINDS);
      // Mostly monotonic (ts,seq) — the realistic live-append case — but
      // occasionally dip below the current max to exercise the rare
      // out-of-order splice path in turnNodeTable.ts.
      const ts = rng() < 0.15 && nextTs > 1000 ? nextTs - Math.floor(rng() * 3) - 1 : nextTs++;
      const seq = 0;
      tsSeqById.set(id, { ts, seq });
      const n: NodeWire = {
        cv: 1, node_id: id, parent_id: null, turn_id: TURN, surface_id: SURFACE,
        kind, ts, seq, status: pick(rng, STATUSES),
        payload: freshPayload(kind, rng, step),
        run_ref: rng() < 0.3 ? pick(rng, RUNS).run_ref : null,
        sidecar_ref: null, target_ref: null, child_manifest: null,
      };
      upsertTurnNode(table, n);
      eventsCache.delete(id);
      refNodes.set(id, n);
    } else if ((action === "update_existing" || action === "new_node") && usedIds.length > 0) {
      const id = pick(rng, usedIds);
      const prior = refNodes.get(id)!;
      const { ts, seq } = tsSeqById.get(id)!;
      const n: NodeWire = {
        ...prior, ts, seq, status: pick(rng, STATUSES),
        payload: freshPayload(prior.kind, rng, step),
      };
      upsertTurnNode(table, n);
      eventsCache.delete(id);
      refNodes.set(id, n);
    } else if (action === "text_delta") {
      const textIds = usedIds.filter((id) => (TEXT_KINDS as readonly string[]).includes(refNodes.get(id)!.kind));
      if (textIds.length === 0) continue;
      const id = pick(rng, textIds);
      const before = getTurnNode(table, id)!;
      const patched = applyTextDeltaToNode(before, `+delta${step}`);
      upsertTurnNode(table, patched);
      eventsCache.delete(id);
      refNodes.set(id, patched);
    } else if (action === "node_status") {
      const id = pick(rng, usedIds);
      const before = getTurnNode(table, id)!;
      const patched = applyNodeStatusToNode(before, pick(rng, STATUSES)!);
      upsertTurnNode(table, patched);
      eventsCache.delete(id);
      refNodes.set(id, patched);
    } else if (action === "new_subagent_node" && usedSubagentIds.length < SUBAGENT_NODE_ID_POOL.length) {
      // A structural node upsert — must patch exactly like any other kind
      // (turnNodeTable/eventsCache don't special-case it).
      const id = SUBAGENT_NODE_ID_POOL[usedSubagentIds.length];
      usedSubagentIds.push(id);
      const ts = nextTs++;
      const n = subagentTurnNode(id, ts, 0, 1 + Math.floor(rng() * 3));
      tsSeqById.set(id, { ts, seq: 0 });
      upsertTurnNode(table, n);
      eventsCache.delete(id);
      refNodes.set(id, n);
    } else if (action === "resolve_subagent_children" && usedSubagentIds.length > 0) {
      // Lazy children arrival — mirrors maybeFetchSubagentChildren's
      // resolution callback (useSurfaceSession.ts) exactly: populate the
      // children map, then invalidate ONLY this node's cached events.
      const id = pick(rng, usedSubagentIds);
      const children = fabricateSubagentChildren(`${id}-${step}`, 1 + Math.floor(rng() * 2));
      subagentChildren.set(id, children);
      eventsCache.delete(id);
    }

    const incremental = mapTurnToAssistantMessage(turn(), table.nodes, RUNS_BY_ID, eventsCache, subagentChildren);
    const fromScratch = mapTurnToAssistantMessage(
      turn(), [...refNodes.values()], RUNS_BY_ID, undefined, subagentChildren,
    );
    expect(incremental, `seed=${seed} step=${step}`).toEqual(fromScratch);

    // Linking contract, independent of the incremental/from-scratch
    // comparison above: every resolved subagent's child events must carry
    // parent_tool_use_id === the anchor recovered from its own node_id,
    // and exactly one event per fabricated child (each is a single
    // assistant_text node -> exactly one "output" event).
    for (const subagentId of usedSubagentIds) {
      const children = subagentChildren.get(subagentId);
      if (!children) continue;
      const anchor = parseSubagentAnchorToolUseId(subagentId);
      expect(anchor).not.toBeNull();
      const childIds = new Set(children.map((c) => c.node_id));
      const linked = incremental.events.filter((e) => childIds.has(e.data?.node_id as string | undefined ?? ""));
      expect(linked, `subagent=${subagentId}`).toHaveLength(children.length);
      for (const e of linked) expect(e.data?.parent_tool_use_id).toBe(anchor);
    }
  }

  // Final node set, one more time via a brand-new cache-less table built
  // straight from the incrementally-maintained node list — the literal
  // "final node set" parity claim.
  const finalFromScratch = mapTurnToAssistantMessage(
    turn(), [...refNodes.values()], RUNS_BY_ID, undefined, subagentChildren,
  );
  const finalIncremental = mapTurnToAssistantMessage(
    turn(), table.nodes, RUNS_BY_ID, eventsCache, subagentChildren,
  );
  expect(finalIncremental).toEqual(finalFromScratch);
}

describe("incremental turnNodeTable/eventsCache parity with from-scratch mapTurnToAssistantMessage", () => {
  const SEEDS = [1, 42, 1337, 987654321, 7];
  for (const seed of SEEDS) {
    it(`matches from-scratch mapping at every step (seed=${seed})`, () => {
      runScenario(seed, 120);
    });
  }
});
