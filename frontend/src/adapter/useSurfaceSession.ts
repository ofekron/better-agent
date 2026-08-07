// Wires SurfaceClient + mapToRenderModel into the SAME state-update entry
// points useSession.ts already exposes:
//   - full-snapshot fetches (initial hydration, and any refetch after a
//     `resync_required` frame) -> `onSnapshot`, delegates to useSession's
//     `replaceMessages` (wholesale swap — correct here because surface-v2
//     message ids live in their own namespace, see mapToRenderModel.ts;
//     there is nothing to id-merge against).
//   - one live frame -> `onUpsertMessage`, delegates to useSession's
//     `applyMessagesReplay` (id-based upsert — exactly the semantics one
//     recomputed ChatMessage needs).
//
// This hook owns no React state of its own; it keeps a plain mutable
// per-turn node table in a ref/closure (never exposed) and pushes fully-
// formed ChatMessage objects out through the two callbacks above. See
// frontend/src/hooks/useSession.ts for how the two callbacks are wired
// and gated behind the `ba.surface_v2` flag (flag.ts).

import { useEffect, useRef, useState } from "react";
import type { ChatMessage } from "../types";
import { SurfaceClient, type SurfaceSocket } from "./client";
import {
  applyNodeStatusToNode,
  applyTextDeltaToNode,
  mapInstructionWidgetToMessage,
  mapPromptToUserMessage,
  mapTurnToAssistantMessage,
  mapTurnToMessages,
  turnPhaseToMessagePatch,
  type EventsCache,
} from "./mapToRenderModel";
import {
  buildTurnNodeTable,
  createTurnNodeTable,
  getTurnNode,
  hasTurnNode,
  upsertTurnNode,
  type TurnNodeTable,
} from "./turnNodeTable";
import type { ChatFrame, CompactTurnWire, NodeWire, RunWire, SnapshotIdentity } from "./wire";

export interface UseSurfaceSessionOptions {
  /** Session to hydrate/live-subscribe, or null to disable entirely — the
   * OFF path performs zero fetches and opens no socket. */
  sessionId: string | null;
  onSnapshot: (sessionId: string, messages: ChatMessage[]) => void;
  onUpsertMessage: (sessionId: string, message: ChatMessage) => void;
}

interface TurnState {
  turn: CompactTurnWire;
  /** Incrementally-maintained node table — see turnNodeTable.ts. Mutated
   * in place by every live frame below; never handed to React state
   * directly (mapTurnToAssistantMessage/mapTurnToMessages always copy
   * `.nodes` before deriving a ChatMessage). */
  body: TurnNodeTable;
  /** node_id -> memoized mapNodeToEvents() output for this turn's body,
   * kept in lockstep with `body`: every mutation to a node's content
   * deletes that node_id's entry here (see EventsCache in
   * mapToRenderModel.ts) so the next derive recomputes only that one
   * node's events, not the whole turn's. */
  eventsCache: EventsCache;
  /** native_subagent_turn node_id -> its resolved child NodeWire[] (see
   * SubagentChildrenByNodeId / maybeFetchSubagentChildren below). Absent
   * entry = not fetched yet. */
  subagentChildren: Map<string, NodeWire[]>;
  /** native_subagent_turn node_id -> the child_manifest.renderable_child_
   * count a fetch was last triggered for — dedup gate so a repeated
   * node_upsert for the same node (status changes, etc.) doesn't refetch
   * unless the manifest actually reports a NEW count (the subagent
   * produced more children since the last fetch). */
  subagentFetchTriggeredCount: Map<string, number>;
}

function byTsSeq(a: { ts: number; seq: number }, b: { ts: number; seq: number }): number {
  return a.ts - b.ts || a.seq - b.seq;
}

/** Single source of truth for a freshly-created TurnState's shape — every
 * creation site (hydrate's bulk load, a live typed_prompt's scaffolded
 * turn) goes through this. */
function newTurnState(turn: CompactTurnWire, body: TurnNodeTable): TurnState {
  return {
    turn,
    body,
    eventsCache: new Map(),
    subagentChildren: new Map(),
    subagentFetchTriggeredCount: new Map(),
  };
}

/** Fabricates a minimal `turn` Node scaffold for a turn discovered live
 * (its typed_prompt arrived before any snapshot fetch established one).
 * Structural kinds carry no payload/status (nodes.py); this scaffold is
 * never sent over the wire, only fed into mapTurnToAssistantMessage,
 * which reads just `ts`/`seq`/`status`/`turn_id` off it. */
function scaffoldTurn(promptNode: NodeWire): CompactTurnWire {
  return {
    turn: {
      ...promptNode,
      node_id: `turn:${promptNode.turn_id}`,
      kind: "turn",
      payload: null,
      status: null,
      child_manifest: null,
    },
    prompt: promptNode,
    results: [],
    manifest: { renderable_child_count: 0, has_children: false },
    runtime_change: null,
  };
}

export function useSurfaceSession(options: UseSurfaceSessionOptions): void {
  const optionsRef = useRef(options);
  useEffect(() => {
    optionsRef.current = options;
  }, [options]);
  const [client] = useState(() => new SurfaceClient());

  const sessionId = options.sessionId;

  useEffect(() => {
    if (!sessionId) return;
    const sid: string = sessionId;
    let cancelled = false;
    let socket: SurfaceSocket | null = null;
    let socketOpened = false;
    let identity: SnapshotIdentity | null = null;
    const turnsById = new Map<string, TurnState>();
    const runsById = new Map<string, RunWire>();
    // Session-level (not per-turn) — see mapInstructionWidgetToMessage.
    let instructionWidgetNode: NodeWire | null = null;

    function currentCursor() {
      if (!identity) return null;
      return { surface_id: sid, incarnation: identity.incarnation, render_rev: identity.render_rev };
    }

    function pushSnapshot(): void {
      if (cancelled) return;
      const ordered = [...turnsById.values()].sort((a, b) => byTsSeq(a.turn.turn, b.turn.turn));
      // From-scratch (no eventsCache): snapshot/resync always fully re-derives.
      const messages = ordered.flatMap((t) => mapTurnToMessages(t.turn, t.body.nodes, runsById));
      const withBanner = instructionWidgetNode
        ? [mapInstructionWidgetToMessage(instructionWidgetNode), ...messages]
        : messages;
      optionsRef.current.onSnapshot(sid, withBanner);
    }

    function pushUpsert(turnId: string): void {
      if (cancelled) return;
      const state = turnsById.get(turnId);
      if (!state) return;
      optionsRef.current.onUpsertMessage(
        sid,
        mapTurnToAssistantMessage(
          state.turn,
          state.body.nodes,
          runsById,
          state.eventsCache,
          state.subagentChildren,
        ),
      );
    }

    /** Kicks off (at most once per manifest count) the SAME turn-body-
     * resolution call used for compact turns (SurfaceClient.fetchTurnBody
     * — a subagent turn's body has the identical explanation-wrapped
     * shape) for a native_subagent_turn node, caches the result, and
     * re-derives/pushes the owning turn once it lands. No-ops for every
     * other kind, an already-fetched-at-this-count node, or a manifest
     * reporting zero children. */
    function maybeFetchSubagentChildren(turnId: string, state: TurnState, node: NodeWire): void {
      if (node.kind !== "native_subagent_turn") return;
      const count = node.child_manifest?.renderable_child_count ?? 0;
      if (count <= 0) return;
      if (state.subagentFetchTriggeredCount.get(node.node_id) === count) return;
      state.subagentFetchTriggeredCount.set(node.node_id, count);
      const atRenderRev = identity?.render_rev ?? 0;
      void client.fetchTurnBody(sid, node.node_id, atRenderRev).then((children) => {
        if (cancelled) return;
        state.subagentChildren.set(node.node_id, children);
        state.eventsCache.delete(node.node_id);
        pushUpsert(turnId);
      });
    }

    async function hydrate(): Promise<void> {
      const envelope = await client.fetchSnapshot(sid);
      if (cancelled) return;
      if (envelope.kind === "stale_cursor") return;
      if (envelope.kind === "rebuilding") {
        setTimeout(() => {
          if (!cancelled) void hydrate();
        }, envelope.retry_after_ms ?? 500);
        return;
      }

      identity = envelope.snapshot_identity;
      turnsById.clear();
      runsById.clear();
      for (const run of envelope.runs) runsById.set(run.run_ref, run);
      instructionWidgetNode = envelope.instruction_widget ?? null;

      const renderRev = identity.render_rev;
      const bodies = await Promise.all(
        envelope.turns.map((turn, i): Promise<NodeWire[]> => {
          const isLastTurn = i === envelope.turns.length - 1;
          if (isLastTurn) return Promise.resolve(envelope.live_turn_nodes);
          if (turn.manifest.renderable_child_count > 0) {
            return client.fetchTurnBody(sid, turn.turn.node_id, renderRev);
          }
          return Promise.resolve([]);
        }),
      );
      if (cancelled) return;
      for (let i = 0; i < envelope.turns.length; i++) {
        const turnId = envelope.turns[i].turn.turn_id;
        const state = newTurnState(envelope.turns[i], buildTurnNodeTable(bodies[i]));
        turnsById.set(turnId, state);
        for (const node of bodies[i]) maybeFetchSubagentChildren(turnId, state, node);
      }

      pushSnapshot();
      const cursor = currentCursor();
      if (socket && cursor) {
        if (!socketOpened) {
          socket.open([cursor], "opened");
          socketOpened = true;
        } else {
          socket.updateCursors([cursor]);
        }
      }
    }

    socket = client.openSocket({
      onFrame: (frame: ChatFrame) => {
        if (!identity) return; // pre-hydration frames: hydrate() replays from the journal anyway
        switch (frame.type) {
          case "node_upsert": {
            identity = frame.snapshot;
            const cursor = currentCursor();
            if (socket && cursor) socket.trackCursor([cursor]);
            const node = frame.node;
            if (node.kind === "instruction_widget") {
              instructionWidgetNode = node;
              optionsRef.current.onUpsertMessage(sid, mapInstructionWidgetToMessage(node));
              return;
            }
            const existing = turnsById.get(node.turn_id);
            if (!existing) {
              if (node.kind !== "typed_prompt") return; // no anchoring turn yet
              turnsById.set(node.turn_id, newTurnState(scaffoldTurn(node), createTurnNodeTable()));
              pushUpsert(node.turn_id);
              return;
            }
            if (node.kind === "typed_prompt") {
              turnsById.set(node.turn_id, { ...existing, turn: { ...existing.turn, prompt: node } });
              optionsRef.current.onUpsertMessage(sid, mapPromptToUserMessage(node));
              return;
            }
            if (node.kind === "result") {
              const results = [...existing.turn.results.filter((n) => n.node_id !== node.node_id), node];
              turnsById.set(node.turn_id, { ...existing, turn: { ...existing.turn, results } });
            } else {
              // Mutates existing.body/eventsCache in place (see
              // turnNodeTable.ts) — O(1) amortized, no new TurnState
              // needed since `existing` IS the stored object.
              upsertTurnNode(existing.body, node);
              existing.eventsCache.delete(node.node_id);
              maybeFetchSubagentChildren(node.turn_id, existing, node);
            }
            pushUpsert(node.turn_id);
            return;
          }
          case "text_delta": {
            for (const [turnId, state] of turnsById) {
              if (!hasTurnNode(state.body, frame.node_id)) continue;
              const node = getTurnNode(state.body, frame.node_id)!;
              upsertTurnNode(state.body, applyTextDeltaToNode(node, frame.appended_text));
              state.eventsCache.delete(frame.node_id);
              pushUpsert(turnId);
              return;
            }
            return;
          }
          case "node_status": {
            for (const [turnId, state] of turnsById) {
              if (!hasTurnNode(state.body, frame.node_id)) continue;
              const node = getTurnNode(state.body, frame.node_id)!;
              upsertTurnNode(state.body, applyNodeStatusToNode(node, frame.status));
              state.eventsCache.delete(frame.node_id);
              pushUpsert(turnId);
              return;
            }
            return;
          }
          case "turn_lifecycle": {
            const state = turnsById.get(frame.turn_id);
            if (!state) return;
            const base = mapTurnToAssistantMessage(
              state.turn,
              state.body.nodes,
              runsById,
              state.eventsCache,
              state.subagentChildren,
            );
            optionsRef.current.onUpsertMessage(sid, { ...base, ...turnPhaseToMessagePatch(frame) });
            return;
          }
          case "run_upsert": {
            runsById.set(frame.run.run_ref, frame.run);
            return;
          }
          // approval_upsert / sidecar_upsert / session_state / notice:
          // not part of the CHAT content plane's ChatMessage model in
          // this phase — see mapToRenderModel.ts's gap list.
          default:
            return;
        }
      },
      onResyncRequired: () => {
        void hydrate();
      },
    });

    void hydrate();

    return () => {
      cancelled = true;
      socket?.close();
      socket = null;
    };
  }, [sessionId, client]);
}
