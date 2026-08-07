// Pure mapping: Chat Surface Contract v2 nodes/turns -> the EXISTING
// legacy render model (ChatMessage + flat WSEvent shapes), so
// MessageBubble / agentMessages.ts render surface-v2 content unchanged.
//
// Mapping table (NodeKind -> legacy shape):
//   typed_prompt        -> a role:"user" ChatMessage
//   assistant_text      -> WSEvent{type:"output", data:{output, node_id}}
//   thinking            -> WSEvent{type:"thinking", data:{thought, node_id}}
//   tool_interaction     -> WSEvent{type:"tool_call", ...} (+ a paired
//                          WSEvent{type:"tool_result", ...} when
//                          payload.result is set — legacy's flattened
//                          "tool_interaction -> tool_call + tool_result"
//                          shape, produced here directly instead of at
//                          render time)
//   model_change(source=provider) -> WSEvent{type:"model_fallback", ...}
//   model_change(source=user)   -> WSEvent{type:"model_switched", ...},
//                          reusing the legacy turn-boundary model-switch
//                          banner (MessageBubble ModelSwitchedEvent /
//                          ModelSwitchBoundaryEvents). ModelChangePayload
//                          only carries display model-name strings (see
//                          wire.ts), so only `model`/`previous_model` are
//                          populated from the wire itself; provider_id/
//                          runner/reasoning_effort are best-effort filled
//                          from the node's own run (node.run_ref ->
//                          runsById) when present, empty otherwise.
//   harness_change       -> WSEvent{type:"model_switched", data:{changed:
//                          ["harness_profile"], ...}} — the SAME boundary
//                          rendering path as model_change(user), with a
//                          dedicated harness-change label/branch inside
//                          ModelSwitchedEvent (no legacy equivalent to
//                          reuse otherwise).
//   diagnostic           -> WSEvent{type:"diagnostic", ...}
//   failure              -> WSEvent{type:"failure", data:{code, text,
//                          severity, retryable, resolution, raw}} — a
//                          first-class event (MessageBubble.FailureChip),
//                          not folded into the generic diagnostic chip.
//                          code="provider_error" ALSO patches
//                          ChatMessage.error/errorText (legacy top-of-
//                          bubble error banner, kept as-is); every other
//                          code renders ONLY via the FailureChip so the
//                          same failure never double-renders.
//   lifecycle_notice     -> WSEvent{type:"lifecycle_notice", ...} (exact
//                          legacy shape — flattenClaudeMessages already
//                          special-cases this mtype identically)
//   compaction           -> WSEvent{type:"diagnostic", data:{kind:"compaction",...}}
//                          (no dedicated legacy visual; diagnostic fallback)
//   fact(kind="pr_link")  -> WSEvent{type:"pr_link", ...}
//   fact(other kinds)     -> WSEvent{type:"diagnostic", data:{kind:"fact.<kind>",...}}
//                          (no generic legacy "fact chip" renderer yet)
//   user_interaction      -> WSEvent{type:"diagnostic", data:{kind:
//                          "user_interaction", raw:node.payload}} always
//                          (unchanged mapping). Render-layer split (see
//                          isActionableUserInteractionEvent /
//                          mapUserInteractionEventToRequest below): a
//                          pending node whose `kind` corresponds to an
//                          existing legacy request shape (input/approval/
//                          memory) is surfaced by Chat.tsx as the SAME
//                          UserInputCard/UserApprovalCard/
//                          MemoryProposalCard used for legacy pending
//                          requests (resolution still POSTs through those
//                          components' own legacy endpoints — no new
//                          interaction flow); everything else (unknown
//                          kind, or resolved/cancelled state) renders the
//                          inline diagnostic fallback with a pending/
//                          resolved state chip.
//   unknown               -> WSEvent{type:"diagnostic", data:{kind:"unknown.<label>",...}}
//                          (the existing unknown-event rendering path)
//   worker_interaction     -> folded into ChatMessage.workers (WorkerPanel[])
//                          by fact_kind (worker_start/worker_event/worker_complete)
//   worker_turn / native_subagent_turn / sub_session_turn / session_turn
//                        -> GAP (documented, not a silent drop): these are
//                          structural containers with NO payload — a full
//                          WorkerPanel needs their `sidecar_ref` resolved
//                          via ChatSurface.fetch_sidecar, which
//                          backend/adapters/chat_adapter.py currently
//                          returns Rebuilding for unconditionally (not
//                          implemented yet). Left unmapped here rather
//                          than faked.
//   instruction_widget    -> session-level (one per snapshot, not
//                          per-turn) -> a synthetic ChatMessage with a
//                          fixed id (surfaceInstructionWidgetMessageId)
//                          outside the turn_id namespace, prepended by
//                          useSurfaceSession.ts ahead of every turn
//                          message. Chat.tsx recognizes the fixed id,
//                          excludes it from turn-pairing (it is never a
//                          Turn), and renders it as a standalone banner.
//   turn / explanation / result -> structural; never produce a WSEvent
//                          directly. `turn` seeds the assistant
//                          ChatMessage's timestamp/status; `result`
//                          finalizes it (isStreaming/completed_at/content);
//                          `explanation` is transparently unwrapped by
//                          `SurfaceClient.fetchTurnBody`, never seen here.
//   continuation_session   -> patches ChatMessage.continuation_active
//                          (chain_depth) — not a WSEvent, an existing
//                          dedicated ChatMessage field.
//   steering_message       -> WSEvent{type:"steer_prompt", data:{text,target}}

import type {
  ChatMessage,
  MemoryProposal,
  MemoryProposalRequest,
  MessageFile,
  MessageImage,
  ProviderRunner,
  UserApprovalRequest,
  UserInputQuestion,
  UserInputRequest,
  UserInteractionRequest,
  WorkerPanel,
  WSEvent,
} from "../types";
import type {
  AssistantTextPayloadWire,
  CompactionPayloadWire,
  CompactTurnWire,
  ContentStatusWire,
  ContinuationSessionPayloadWire,
  DiagnosticPayloadWire,
  FactPayloadWire,
  FailurePayloadWire,
  HarnessChangePayloadWire,
  InstructionWidgetPayloadWire,
  LifecycleNoticePayloadWire,
  ModelChangePayloadWire,
  NodeWire,
  ResultPayloadWire,
  RunWire,
  SteeringMessagePayloadWire,
  ThinkingPayloadWire,
  ToolInteractionPayloadWire,
  TurnLifecycleFrame,
  TypedPromptPayloadWire,
  UnknownPayloadWire,
  UserInteractionPayloadWire,
  WorkerInteractionPayloadWire,
} from "./wire";

export type RunsById = ReadonlyMap<string, RunWire>;

// ---- deterministic ids --------------------------------------------------
//
// Legacy ChatMessage ids are backend-generated uuid4()s with NO derivable
// relationship to the contract's node_id space (confirmed against
// backend/orchestrator.py's user/assistant scaffold construction). Surface
// v2 messages therefore live in their own namespaced id space; callers
// must fully replace (never id-merge with) whatever legacy messages were
// present for a surface-owned session — see useSurfaceSession.ts.

export function surfaceUserMessageId(turnId: string): string {
  return `surface:${turnId}:prompt`;
}

export function surfaceAssistantMessageId(turnId: string): string {
  return `surface:${turnId}:assistant`;
}

/** Fixed, session-scoped (not per-turn) id for the instruction_widget
 * banner message — a singleton outside the turn_id namespace, so the
 * caller can recognize and exclude it from turn-pairing by id alone. */
export function surfaceInstructionWidgetMessageId(): string {
  return "surface:instruction_widget";
}

function tsToIso(ts: number): string {
  return new Date(ts * 1000).toISOString();
}

function sortNodes(nodes: NodeWire[]): NodeWire[] {
  return [...nodes].sort((a, b) => a.ts - b.ts || a.seq - b.seq);
}

// ---- node_id-keyed upsert (mirrors Strategy.ts's uuid dedup, but keyed
// by the contract's node_id, stamped into every mapped event's
// `data.node_id`) -----------------------------------------------------

export function upsertEventByNodeId(events: WSEvent[], event: WSEvent): WSEvent[] {
  const nodeId = (event.data as { node_id?: unknown } | undefined)?.node_id;
  if (typeof nodeId !== "string") return [...events, event];
  const idx = events.findIndex(
    (e) => (e.data as { node_id?: unknown } | undefined)?.node_id === nodeId,
  );
  if (idx === -1) return [...events, event];
  const next = [...events];
  next[idx] = event;
  return next;
}

// ---- Node -> flat WSEvent -------------------------------------------

function toolResultOutput(result: ToolInteractionPayloadWire["result"]): string {
  if (!result) return "";
  const output = (result as { output?: unknown }).output;
  if (typeof output === "string") return output;
  try {
    return JSON.stringify(result);
  } catch {
    return String(result);
  }
}

/** Maps one content node to zero or more flat legacy WSEvents. Most kinds
 * produce exactly one; `tool_interaction` with a resolved result produces
 * two (tool_call + tool_result, mirroring how the legacy render pipeline
 * flattens a single provider tool_use+tool_result pair today). Returns
 * `[]` for structural/unmapped kinds (see the module-level gap list).
 * `runsById` is used only by `model_change(source=user)` to best-effort
 * fill provider/runner/reasoning_effort for the node's own run — optional
 * and defaulted so every other call site (and existing tests) is
 * unaffected. */
export function mapNodeToEvents(node: NodeWire, runsById: RunsById = new Map()): WSEvent[] {
  const base = { node_id: node.node_id };
  switch (node.kind) {
    case "assistant_text": {
      const p = node.payload as AssistantTextPayloadWire;
      return [{ type: "output", data: { ...base, output: p.text, parent_tool_use_id: null } }];
    }
    case "thinking": {
      const p = node.payload as ThinkingPayloadWire;
      return [{ type: "thinking", data: { ...base, thought: p.text, parent_tool_use_id: null } }];
    }
    case "tool_interaction": {
      const p = node.payload as ToolInteractionPayloadWire;
      const events: WSEvent[] = [
        {
          type: "tool_call",
          data: { ...base, tool: p.tool_name, args: p.args, tool_use_id: node.node_id, parent_tool_use_id: null },
        },
      ];
      if (p.result) {
        events.push({
          type: "tool_result",
          data: {
            ...base,
            output: toolResultOutput(p.result),
            tool_use_id: node.node_id,
            paired_tool_result: true,
            orphan_tool_result: false,
          },
        });
      }
      return events;
    }
    case "steering_message": {
      const p = node.payload as SteeringMessagePayloadWire;
      return [{ type: "steer_prompt", data: { ...base, text: p.text, target: p.target } }];
    }
    case "model_change": {
      const p = node.payload as ModelChangePayloadWire;
      if (p.source === "provider") {
        return [
          {
            type: "model_fallback",
            data: { ...base, from_model: p.from_run_ref ?? "", to_model: p.to_run_ref },
          },
        ];
      }
      // source === "user": onto the legacy turn-boundary model-switch
      // banner. See the module docstring for the data-fidelity caveat.
      const run = node.run_ref ? runsById.get(node.run_ref) : undefined;
      return [
        {
          type: "model_switched",
          data: {
            ...base,
            model: p.to_run_ref,
            previous_model: p.from_run_ref ?? "",
            provider_id: run?.provider_id ?? "",
            reasoning_effort: run?.reasoning_effort ?? "",
            runner: run?.runner ?? "",
            changed: ["model"],
          },
        },
      ];
    }
    case "harness_change": {
      const p = node.payload as HarnessChangePayloadWire;
      return [
        {
          type: "model_switched",
          data: {
            ...base,
            changed: ["harness_profile"],
            harness_profile_id: p.to_harness_profile_id,
            previous_harness_profile_id: p.from_harness_profile_id ?? "",
          },
        },
      ];
    }
    case "lifecycle_notice": {
      const p = node.payload as LifecycleNoticePayloadWire;
      // `kind` (the discriminant LifecycleNotice.tsx dispatches on) is
      // spread LAST so a `p.data` field of the same name can never shadow
      // it — a nested-data collision would silently misroute rendering.
      return [{ type: "lifecycle_notice", data: { ...base, ...(p.data ?? {}), kind: p.kind } }];
    }
    case "compaction": {
      const p = node.payload as CompactionPayloadWire;
      return [
        {
          type: "diagnostic",
          data: { ...base, kind: "compaction", origin: p.origin, summary: p.summary },
        },
      ];
    }
    case "failure": {
      const p = node.payload as FailurePayloadWire;
      return [
        {
          type: "failure",
          data: {
            ...base,
            code: p.code,
            text: p.text,
            severity: p.severity,
            retryable: p.retryable,
            resolution: p.resolution,
            raw: p.data,
          },
        },
      ];
    }
    case "diagnostic": {
      const p = node.payload as DiagnosticPayloadWire;
      return [
        { type: "diagnostic", data: { ...base, kind: p.code, severity: p.severity, text: p.text, raw: p.data } },
      ];
    }
    case "fact": {
      const p = node.payload as FactPayloadWire;
      if (p.kind === "pr_link") return [{ type: "pr_link", data: { ...base, ...p.data } }];
      return [{ type: "diagnostic", data: { ...base, kind: `fact.${p.kind}`, raw: p.data } }];
    }
    case "user_interaction": {
      return [{ type: "diagnostic", data: { ...base, kind: "user_interaction", raw: node.payload } }];
    }
    case "unknown": {
      const p = node.payload as UnknownPayloadWire;
      return [{ type: "diagnostic", data: { ...base, kind: `unknown.${p.label}`, raw: p.payload } }];
    }
    // Structural / handled-elsewhere kinds, plus the one remaining
    // documented GAP (the sidecar-dependent SubAgentTurn family) — see
    // the module docstring: no direct WSEvent.
    case "turn":
    case "explanation":
    case "result":
    case "typed_prompt":
    case "worker_interaction":
    case "continuation_session":
    case "instruction_widget":
    case "worker_turn":
    case "native_subagent_turn":
    case "sub_session_turn":
    case "session_turn":
      return [];
    default:
      return [];
  }
}

// ---- typed_prompt -> user ChatMessage --------------------------------

export function mapPromptToUserMessage(prompt: NodeWire): ChatMessage {
  const p = prompt.payload as TypedPromptPayloadWire;
  const images: MessageImage[] = [];
  const files: MessageFile[] = [];
  for (const a of p.attachments) {
    if (a.media_type.startsWith("image/")) {
      // `ref` (not `name`) is the resolvable key: adapter_api.py's
      // GET /attachments/{ref} and the legacy GET /sessions/{id}/images/{filename}
      // route both key off it, and MessageBubble's buildMessageImageUrl
      // already resolves MessageImage.filename via the legacy route. Empty
      // when normalize.py couldn't derive a servable location for this
      // image (backend/adapters/normalize.py::_split_image_attachments —
      // the journal row carries the raw bytes but no persisted copy this
      // pure mapping module can point at); buildMessageImageUrl no-ops on
      // an empty filename, so this degrades to no image, never a broken src.
      images.push({ filename: a.ref || undefined, media_type: a.media_type });
    } else {
      // GAP: no write path ever populates a non-empty `ref` for a
      // non-image attachment — file bytes are inlined into the prompt
      // text and discarded (backend/file_attachment_prompt.py), never
      // persisted anywhere a ref could name. `size` is carried through
      // when the backend could derive it; MessageFile.size is non-optional
      // so an undetermined size falls back to 0 rather than widening the
      // type just for this still-unreachable branch.
      files.push({ name: a.name, media_type: a.media_type, size: a.size ?? 0 });
    }
  }
  return {
    id: surfaceUserMessageId(prompt.turn_id),
    role: "user",
    content: p.text,
    cli_prompt: p.sent_text ?? undefined,
    events: [],
    timestamp: tsToIso(prompt.ts),
    isStreaming: false,
    images: images.length > 0 ? images : undefined,
    files: files.length > 0 ? files : undefined,
  };
}

// ---- instruction_widget -> synthetic banner ChatMessage ----------------

/** Maps the snapshot-level instruction_widget node to a synthetic
 * ChatMessage carrying only its display text — never a Turn (see
 * surfaceInstructionWidgetMessageId). `action` (an opaque command payload)
 * is intentionally not rendered here: no legacy interaction flow exists
 * for it yet, and inventing one is out of scope for this mapping. */
export function mapInstructionWidgetToMessage(node: NodeWire): ChatMessage {
  const p = node.payload as InstructionWidgetPayloadWire;
  return {
    id: surfaceInstructionWidgetMessageId(),
    role: "operator",
    content: p.text,
    events: [],
    timestamp: tsToIso(node.ts),
    isStreaming: false,
  };
}

// ---- worker_interaction facts -> WorkerPanel[] ------------------------

interface WorkerStartFact {
  delegation_id: string;
  worker_session_id: string | null;
  worker_description: string;
  panel_kind?: WorkerPanel["panel_kind"];
  started_at?: string;
  insert_at?: number;
  is_new: boolean;
  instructions_preview: string;
  orchestration_mode?: WorkerPanel["orchestration_mode"];
  provider_id?: string | null;
  model?: string | null;
  reasoning_effort?: WorkerPanel["reasoning_effort"];
  run_mode?: string;
}

interface WorkerEventFact {
  delegation_id: string;
  event?: WSEvent;
}

interface WorkerCompleteFact {
  delegation_id: string;
  worker_session_id?: string | null;
  jsonl_path?: string | null;
  new_byte_offset?: number;
  token_usage?: WorkerPanel["token_usage"];
  success?: boolean;
  error?: string | null;
  fork_agent_sid?: string | null;
  run_mode?: string;
}

/** Reduces a turn's WORKER_INTERACTION fact log into WorkerPanel[],
 * mirroring Strategy.ts's worker_start/worker_event/worker_complete live
 * handling — same fact shape, replayed here from journal-sourced nodes
 * instead of one-at-a-time WS frames. */
export function reduceWorkerInteractions(nodes: NodeWire[]): WorkerPanel[] {
  let panels: WorkerPanel[] = [];
  for (const node of sortNodes(nodes)) {
    if (node.kind !== "worker_interaction") continue;
    const p = node.payload as WorkerInteractionPayloadWire;
    if (p.fact_kind === "worker_start") {
      const d = p.fact as unknown as WorkerStartFact;
      if (panels.some((panel) => panel.delegation_id === d.delegation_id)) continue;
      panels = [
        ...panels,
        {
          delegation_id: d.delegation_id,
          worker_session_id: d.worker_session_id ?? "",
          worker_description: d.worker_description,
          panel_kind: d.panel_kind,
          started_at: d.started_at,
          insert_at: d.insert_at,
          is_new: d.is_new,
          instructions_preview: d.instructions_preview,
          orchestration_mode: d.orchestration_mode,
          provider_id: d.provider_id,
          model: d.model,
          reasoning_effort: d.reasoning_effort,
          run_mode: d.run_mode,
          events: [],
        },
      ];
    } else if (p.fact_kind === "worker_event") {
      const d = p.fact as unknown as WorkerEventFact;
      if (!d.event) continue;
      const idx = panels.findIndex((panel) => panel.delegation_id === d.delegation_id);
      if (idx === -1) continue;
      const next = [...panels];
      next[idx] = { ...next[idx], events: upsertEventByNodeId(next[idx].events, d.event) };
      panels = next;
    } else if (p.fact_kind === "worker_complete") {
      const d = p.fact as unknown as WorkerCompleteFact;
      const idx = panels.findIndex((panel) => panel.delegation_id === d.delegation_id);
      if (idx === -1) continue;
      const next = [...panels];
      const panel = { ...next[idx] };
      if (d.worker_session_id) panel.worker_session_id = d.worker_session_id;
      panel.jsonl_path = d.jsonl_path ?? null;
      panel.new_byte_offset = d.new_byte_offset;
      panel.token_usage = d.token_usage;
      panel.success = d.success;
      panel.error = d.error ?? null;
      if (d.fork_agent_sid) panel.fork_agent_sid = d.fork_agent_sid;
      if (d.run_mode) panel.run_mode = d.run_mode;
      next[idx] = panel;
      panels = next;
    }
  }
  return panels;
}

// ---- turn -> assistant ChatMessage ------------------------------------

function statusToStreaming(status: ContentStatusWire | null): boolean {
  return status === "streaming" || status === "queued" || status === "partial";
}

function resultText(results: NodeWire[]): string {
  const marker = results.find((n) => n.kind === "result");
  const markerText = marker ? (marker.payload as ResultPayloadWire).text : null;
  if (markerText) return markerText;
  return results
    .filter((n) => n.kind === "assistant_text")
    .map((n) => (n.payload as AssistantTextPayloadWire).text)
    .join("\n");
}

function resultKindOf(results: NodeWire[]): ResultPayloadWire["result_kind"] | null {
  const marker = results.find((n) => n.kind === "result");
  return marker ? (marker.payload as ResultPayloadWire).result_kind : null;
}

function runMetaFor(
  ordered: NodeWire[],
  runsById: RunsById,
): ChatMessage["run_meta"] {
  const withRun = ordered.find((n) => n.run_ref);
  if (!withRun || !withRun.run_ref) return undefined;
  const run = runsById.get(withRun.run_ref);
  if (!run) return undefined;
  return {
    provider_id: run.provider_id,
    model: run.model,
    reasoning_effort: run.reasoning_effort,
    runner: run.runner as ProviderRunner,
  };
}

/** Builds the assistant-role ChatMessage for one compact turn, given its
 * fully-resolved body (see SurfaceClient.fetchTurnBody). `results` (part
 * of the CompactTurn itself, no extra fetch) supplies the finalized text
 * and completion marker per backend/adapters/derive.py::resolve_result.
 * `runsById` (from the snapshot's/live `run_upsert`-fed `runs[]`) is used
 * only to stamp the existing `run_meta` display field — best-effort, a
 * missing entry just omits it. */
export function mapTurnToAssistantMessage(
  turn: CompactTurnWire,
  bodyNodes: NodeWire[],
  runsById: RunsById = new Map(),
): ChatMessage {
  const ordered = sortNodes(bodyNodes);
  const events = ordered.flatMap((n) => mapNodeToEvents(n, runsById));
  const workers = reduceWorkerInteractions(ordered);
  const runtimeChangeEvents = turn.runtime_change ? mapNodeToEvents(turn.runtime_change, runsById) : [];
  const allEvents = [...runtimeChangeEvents, ...events];

  const bodyText = ordered
    .filter((n) => n.kind === "assistant_text")
    .map((n) => (n.payload as AssistantTextPayloadWire).text)
    .join("\n");
  const finalText = resultText(turn.results) || bodyText;

  const isTerminal = resultKindOf(turn.results) !== null;
  const isStreaming = !isTerminal && statusToStreaming(turn.turn.status);
  const failed = ordered.some((n) => n.kind === "failure");
  const failureNode = ordered.find((n) => n.kind === "failure");

  const msg: ChatMessage = {
    id: surfaceAssistantMessageId(turn.turn.turn_id),
    role: "assistant",
    content: finalText,
    events: allEvents,
    timestamp: tsToIso(turn.turn.ts),
    isStreaming,
    workers: workers.length > 0 ? workers : undefined,
    run_meta: runMetaFor(ordered, runsById) ?? undefined,
  };
  if (isTerminal) msg.completed_at = tsToIso(turn.turn.ts);
  if (turn.turn.status === "stopped") msg.stopped_at = tsToIso(turn.turn.ts);
  if (failed && failureNode) {
    const p = failureNode.payload as FailurePayloadWire;
    // Only `provider_error` keeps patching the legacy top-of-bubble
    // error banner (msg.error/errorText) — every other code renders
    // exclusively via the FailureChip event (mapNodeToEvents above) so
    // the same failure never shows twice.
    if (p.code === "provider_error") {
      msg.error = true;
      msg.errorText = p.text;
    }
  }
  const continuation = ordered.find((n) => n.kind === "continuation_session");
  if (continuation) {
    const p = continuation.payload as ContinuationSessionPayloadWire;
    msg.continuation_active = p.chain_depth;
  }
  return msg;
}

/** Full mapping for one compact turn: the user prompt message (when
 * present — a turn always has one in steady state per chat_adapter.py's
 * segmentation, but a still-forming live turn may not yet) plus the
 * assistant message. Ordering: caller concatenates in (ts,seq) turn
 * order; within a turn, prompt always precedes its assistant reply. */
export function mapTurnToMessages(
  turn: CompactTurnWire,
  bodyNodes: NodeWire[],
  runsById: RunsById = new Map(),
): ChatMessage[] {
  const out: ChatMessage[] = [];
  if (turn.prompt) out.push(mapPromptToUserMessage(turn.prompt));
  out.push(mapTurnToAssistantMessage(turn, bodyNodes, runsById));
  return out;
}

// ---- live-frame node-table patches --------------------------------------
//
// useSurfaceSession.ts keeps each turn's body as NodeWire[] (the same
// shape hydration produces) and re-derives the assistant ChatMessage from
// scratch on every frame via mapTurnToAssistantMessage — these two
// helpers patch that node table in place for the two frame kinds whose
// payload isn't itself a full Node.

/** `text_delta` targets `assistant_text`/`thinking` nodes — the only two
 * kinds whose payload carries a plain `text` field appended to
 * incrementally by the provider stream. */
export function applyTextDeltaToNode(node: NodeWire, appendedText: string): NodeWire {
  if (node.kind !== "assistant_text" && node.kind !== "thinking") return node;
  const payload = node.payload as AssistantTextPayloadWire | ThinkingPayloadWire;
  return { ...node, payload: { ...payload, text: payload.text + appendedText } };
}

export function applyNodeStatusToNode(node: NodeWire, status: NodeWire["status"]): NodeWire {
  return node.status === status ? node : { ...node, status };
}

export function turnPhaseToMessagePatch(
  frame: Pick<TurnLifecycleFrame, "phase" | "reason" | "usage">,
): Partial<ChatMessage> {
  const patch: Partial<ChatMessage> = {};
  if (frame.phase === "completed" || frame.phase === "failed" || frame.phase === "stopped") {
    patch.isStreaming = false;
  } else if (frame.phase === "running" || frame.phase === "starting" || frame.phase === "queued") {
    patch.isStreaming = true;
  }
  if (frame.phase === "stopped") patch.stopped_at = new Date().toISOString();
  if (frame.phase === "completed") patch.completed_at = new Date().toISOString();
  if (frame.phase === "failed") {
    patch.error = true;
    if (frame.reason) patch.errorText = frame.reason;
  }
  if (frame.usage) {
    patch.tokenUsage = {
      input_tokens: frame.usage.input_tokens ?? 0,
      output_tokens: frame.usage.output_tokens ?? 0,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 0,
    };
  }
  return patch;
}

// ---- user_interaction -> existing legacy interactive components --------
//
// A pending user_interaction node whose `kind` corresponds to an existing
// legacy request shape is surfaced by Chat.tsx as an actionable card
// (UserInputCard / UserApprovalCard / MemoryProposalCard) merged into the
// same list that already renders legacy pending requests — resolution
// still travels those components' own legacy REST endpoints, no new
// interaction flow. Everything else (unresolved-shape kind, or a
// resolved/cancelled node kept for history) stays the inline diagnostic
// fallback (mapNodeToEvents' unconditional "user_interaction" mapping),
// rendered with a state chip by MessageBubble's diagnostic dispatch.
//
// NodeWire's user_interaction payload has no producer in the backend yet
// (no construction site under backend/adapters/{normalize,derive}.py at
// the time of this mapping — grep-confirmed), so the `kind` string
// vocabulary below (input/approval/memory) is inferred from the existing
// frontend UserInteractionRequest union it is meant to reuse, not
// verified against real wire data. See the Phase E report for this gap.

const CORRESPONDING_USER_INTERACTION_KINDS = new Set(["input", "approval", "memory"]);

function isCorrespondingUserInteractionKind(kind: unknown): kind is "input" | "approval" | "memory" {
  return typeof kind === "string" && CORRESPONDING_USER_INTERACTION_KINDS.has(kind);
}

/** True when a `user_interaction` diagnostic event (as produced by
 * mapNodeToEvents) should render as an actionable legacy card instead of
 * the inline diagnostic fallback. */
export function isActionableUserInteractionEvent(event: WSEvent): boolean {
  if (event.type !== "diagnostic") return false;
  const data = event.data as { kind?: unknown; raw?: unknown } | undefined;
  if (data?.kind !== "user_interaction") return false;
  const raw = data.raw as UserInteractionPayloadWire | undefined;
  return !!raw && raw.state === "pending" && isCorrespondingUserInteractionKind(raw.kind);
}

/** Maps an actionable user_interaction diagnostic event (see
 * isActionableUserInteractionEvent — call that first) to the exact
 * legacy request shape its card component expects. `request` is assumed
 * to already carry the corresponding legacy request's fields (request_id,
 * app_session_id, created_at, plus the kind-specific fields) since it is
 * meant to reuse those shapes; `app_session_id` is left empty (rather
 * than guessed) when absent — the caller decides how to treat that
 * (Chat.tsx trusts the current session, since every node in `messages`
 * is already scoped to one session's subscription). Returns `null` for a
 * non-actionable or unrecognized-kind event. */
export function mapUserInteractionEventToRequest(
  event: WSEvent,
): UserInteractionRequest | null {
  if (!isActionableUserInteractionEvent(event)) return null;
  const data = event.data as { node_id?: unknown; raw?: unknown };
  const raw = data.raw as UserInteractionPayloadWire;
  const request = raw.request;
  const request_id =
    typeof request.request_id === "string" ? request.request_id : String(data.node_id ?? "");
  const app_session_id = typeof request.app_session_id === "string" ? request.app_session_id : "";
  const created_at = typeof request.created_at === "number" ? request.created_at : 0;
  const shared = { request_id, app_session_id, status: "pending" as const, created_at };
  switch (raw.kind) {
    case "approval":
      return {
        ...shared,
        kind: "approval",
        prompt: typeof request.prompt === "string" ? request.prompt : "",
      } satisfies UserApprovalRequest;
    case "memory":
      return {
        ...shared,
        kind: "memory",
        memory_proposal: request.memory_proposal as MemoryProposal,
      } satisfies MemoryProposalRequest;
    case "input":
      return {
        ...shared,
        kind: "input",
        questions: Array.isArray(request.questions) ? (request.questions as UserInputQuestion[]) : [],
      } satisfies UserInputRequest;
    default:
      return null;
  }
}
