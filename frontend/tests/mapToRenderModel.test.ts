import { describe, expect, it } from "vitest";
import {
  applyNodeStatusToNode,
  applyTextDeltaToNode,
  isActionableUserInteractionEvent,
  mapInstructionWidgetToMessage,
  mapNodeToEvents,
  mapPromptToUserMessage,
  mapTurnToAssistantMessage,
  mapTurnToMessages,
  mapUserInteractionEventToRequest,
  reduceWorkerInteractions,
  surfaceAssistantMessageId,
  surfaceInstructionWidgetMessageId,
  surfaceUserMessageId,
  turnPhaseToMessagePatch,
  upsertEventByNodeId,
} from "../src/adapter/mapToRenderModel";
import type {
  CompactTurnWire,
  NodeWire,
  RunWire,
  TurnLifecycleFrame,
} from "../src/adapter/wire";
import type { WSEvent } from "../src/types";

const SURFACE = "s1";
const TURN = "prompt-uuid-1";

function node(overrides: Partial<NodeWire> & Pick<NodeWire, "node_id" | "kind" | "payload">): NodeWire {
  return {
    cv: 1,
    parent_id: null,
    turn_id: TURN,
    surface_id: SURFACE,
    ts: 1000,
    seq: 0,
    status: "complete",
    run_ref: null,
    sidecar_ref: null,
    target_ref: null,
    child_manifest: null,
    ...overrides,
  };
}

describe("mapNodeToEvents", () => {
  it("maps assistant_text to a flat output event carrying node_id", () => {
    const n = node({ node_id: "a1", kind: "assistant_text", payload: { text: "hello" } });
    expect(mapNodeToEvents(n)).toEqual([
      { type: "output", data: { node_id: "a1", output: "hello", parent_tool_use_id: null } },
    ]);
  });

  it("maps thinking to a flat thinking event", () => {
    const n = node({ node_id: "t1", kind: "thinking", payload: { text: "pondering", redacted: false } });
    expect(mapNodeToEvents(n)).toEqual([
      { type: "thinking", data: { node_id: "t1", thought: "pondering", parent_tool_use_id: null } },
    ]);
  });

  it("maps an unresolved tool_interaction to a single tool_call event", () => {
    const n = node({
      node_id: "tool:x",
      kind: "tool_interaction",
      payload: { tool_name: "Bash", args: { cmd: "ls" }, result: null, approval_ref: null, ui_kind: null, derived_view: null },
    });
    expect(mapNodeToEvents(n)).toEqual([
      {
        type: "tool_call",
        data: { node_id: "tool:x", tool: "Bash", args: { cmd: "ls" }, tool_use_id: "tool:x", parent_tool_use_id: null },
      },
    ]);
  });

  it("maps a resolved tool_interaction to a paired tool_call + tool_result", () => {
    const n = node({
      node_id: "tool:x",
      kind: "tool_interaction",
      payload: {
        tool_name: "Bash", args: { cmd: "ls" }, result: { output: "file.txt" },
        approval_ref: null, ui_kind: null, derived_view: null,
      },
    });
    const events = mapNodeToEvents(n);
    expect(events).toHaveLength(2);
    expect(events[0].type).toBe("tool_call");
    expect(events[1]).toEqual({
      type: "tool_result",
      data: { node_id: "tool:x", output: "file.txt", tool_use_id: "tool:x", paired_tool_result: true, orphan_tool_result: false },
    });
  });

  it("maps a provider-sourced model_change to model_fallback", () => {
    const n = node({
      node_id: "mc1", kind: "model_change",
      payload: { from_run_ref: "sonnet-4", to_run_ref: "haiku-4", source: "provider" },
    });
    expect(mapNodeToEvents(n)).toEqual([
      { type: "model_fallback", data: { node_id: "mc1", from_model: "sonnet-4", to_model: "haiku-4" } },
    ]);
  });

  it("maps a user-sourced model_change onto the legacy model_switched boundary event", () => {
    const n = node({
      node_id: "mc2", kind: "model_change",
      payload: { from_run_ref: "sonnet-4", to_run_ref: "haiku-4", source: "user" },
    });
    expect(mapNodeToEvents(n)).toEqual([
      {
        type: "model_switched",
        data: {
          node_id: "mc2",
          model: "haiku-4",
          previous_model: "sonnet-4",
          provider_id: "",
          reasoning_effort: "",
          runner: "",
          changed: ["model"],
        },
      },
    ]);
  });

  it("fills provider_id/reasoning_effort/runner for a user-sourced model_change from the node's own run", () => {
    const run: RunWire = {
      run_ref: "run-1", provider_id: "claude", account_name: null,
      model: "haiku-4", reasoning_effort: "high", runner: "native",
    };
    const n = node({
      node_id: "mc3", kind: "model_change", run_ref: "run-1",
      payload: { from_run_ref: null, to_run_ref: "haiku-4", source: "user" },
    });
    const events = mapNodeToEvents(n, new Map([["run-1", run]]));
    expect(events[0].data).toMatchObject({
      provider_id: "claude", reasoning_effort: "high", runner: "native",
    });
  });

  it("maps harness_change onto the same model_switched boundary event, flagged via `changed`", () => {
    const n = node({
      node_id: "hc1", kind: "harness_change",
      payload: { from_harness_profile_id: "old-profile", to_harness_profile_id: "new-profile" },
    });
    expect(mapNodeToEvents(n)).toEqual([
      {
        type: "model_switched",
        data: {
          node_id: "hc1",
          changed: ["harness_profile"],
          harness_profile_id: "new-profile",
          previous_harness_profile_id: "old-profile",
        },
      },
    ]);
  });

  it("maps lifecycle_notice to the exact legacy shape (kind + spread data)", () => {
    const n = node({
      node_id: "ln1", kind: "lifecycle_notice",
      payload: { kind: "retrying", data: { retry_at: "2026-01-01T00:00:00Z" } },
    });
    expect(mapNodeToEvents(n)).toEqual([
      { type: "lifecycle_notice", data: { node_id: "ln1", kind: "retrying", retry_at: "2026-01-01T00:00:00Z" } },
    ]);
  });

  it("maps failure to a diagnostic-style event", () => {
    const n = node({ node_id: "f1", kind: "failure", payload: { code: "timeout", text: "boom", data: null } });
    expect(mapNodeToEvents(n)).toEqual([
      { type: "diagnostic", data: { node_id: "f1", kind: "failure", code: "timeout", text: "boom", raw: null } },
    ]);
  });

  it("maps fact(pr_link) to a dedicated pr_link event", () => {
    const n = node({ node_id: "pr1", kind: "fact", payload: { kind: "pr_link", data: { number: 42, repo: "x/y" } } });
    expect(mapNodeToEvents(n)).toEqual([
      { type: "pr_link", data: { node_id: "pr1", number: 42, repo: "x/y" } },
    ]);
  });

  it("maps a non-pr_link fact to a diagnostic fallback", () => {
    const n = node({ node_id: "fact1", kind: "fact", payload: { kind: "todo_snapshot", data: { count: 3 } } });
    expect(mapNodeToEvents(n)).toEqual([
      { type: "diagnostic", data: { node_id: "fact1", kind: "fact.todo_snapshot", raw: { count: 3 } } },
    ]);
  });

  it("maps unknown to the existing unknown-event rendering path", () => {
    const n = node({ node_id: "u1", kind: "unknown", payload: { label: "row.mystery", payload: { raw: true } } });
    expect(mapNodeToEvents(n)).toEqual([
      { type: "diagnostic", data: { node_id: "u1", kind: "unknown.row.mystery", raw: { raw: true } } },
    ]);
  });

  it.each([
    "turn", "explanation", "result", "typed_prompt", "worker_interaction",
    "continuation_session", "instruction_widget",
    "worker_turn", "native_subagent_turn", "sub_session_turn", "session_turn",
  ] as const)("produces no direct WSEvent for structural/gap kind %s", (kind) => {
    const n = node({ node_id: "x", kind, payload: null });
    expect(mapNodeToEvents(n)).toEqual([]);
  });

  it("maps user_interaction to a diagnostic event carrying kind/state, unconditionally", () => {
    const n = node({
      node_id: "ui1", kind: "user_interaction",
      payload: { kind: "approval", request: { prompt: "ok?" }, state: "pending", response: null },
    });
    expect(mapNodeToEvents(n)).toEqual([
      {
        type: "diagnostic",
        data: {
          node_id: "ui1", kind: "user_interaction",
          raw: { kind: "approval", request: { prompt: "ok?" }, state: "pending", response: null },
        },
      },
    ]);
  });
});

describe("isActionableUserInteractionEvent / mapUserInteractionEventToRequest", () => {
  function diagnosticEvent(payload: {
    kind: string;
    request: Record<string, unknown>;
    state: "pending" | "resolved" | "cancelled";
  }): WSEvent {
    return {
      type: "diagnostic",
      data: { node_id: "ui-node", kind: "user_interaction", raw: payload },
    };
  }

  it("is actionable for a pending approval-kind node, and maps to a UserApprovalRequest", () => {
    const e = diagnosticEvent({
      kind: "approval",
      request: { request_id: "r1", app_session_id: "s1", created_at: 100, prompt: "Proceed?" },
      state: "pending",
    });
    expect(isActionableUserInteractionEvent(e)).toBe(true);
    expect(mapUserInteractionEventToRequest(e)).toEqual({
      request_id: "r1", app_session_id: "s1", status: "pending", created_at: 100,
      kind: "approval", prompt: "Proceed?",
    });
  });

  it("is actionable for a pending memory-kind node, and maps to a MemoryProposalRequest", () => {
    const proposal = { action: "add", name: "n", description: "d", type: "user", content: "c", scope_type: "global", scope_path: "" };
    const e = diagnosticEvent({
      kind: "memory",
      request: { request_id: "r2", app_session_id: "s1", created_at: 200, memory_proposal: proposal },
      state: "pending",
    });
    expect(isActionableUserInteractionEvent(e)).toBe(true);
    expect(mapUserInteractionEventToRequest(e)).toEqual({
      request_id: "r2", app_session_id: "s1", status: "pending", created_at: 200,
      kind: "memory", memory_proposal: proposal,
    });
  });

  it("is actionable for a pending input-kind node, and maps to a UserInputRequest", () => {
    const questions = [{ id: "q1", header: "H", question: "Q?", options: [] }];
    const e = diagnosticEvent({
      kind: "input",
      request: { request_id: "r3", app_session_id: "s1", created_at: 300, questions },
      state: "pending",
    });
    expect(isActionableUserInteractionEvent(e)).toBe(true);
    expect(mapUserInteractionEventToRequest(e)).toEqual({
      request_id: "r3", app_session_id: "s1", status: "pending", created_at: 300,
      kind: "input", questions,
    });
  });

  it("is not actionable once resolved, even for a corresponding kind", () => {
    const e = diagnosticEvent({
      kind: "approval",
      request: { request_id: "r4", app_session_id: "s1", created_at: 400, prompt: "Proceed?" },
      state: "resolved",
    });
    expect(isActionableUserInteractionEvent(e)).toBe(false);
    expect(mapUserInteractionEventToRequest(e)).toBeNull();
  });

  it("is not actionable for a kind with no corresponding legacy component", () => {
    const e = diagnosticEvent({
      kind: "credential_consent",
      request: { request_id: "r5", app_session_id: "s1", created_at: 500 },
      state: "pending",
    });
    expect(isActionableUserInteractionEvent(e)).toBe(false);
    expect(mapUserInteractionEventToRequest(e)).toBeNull();
  });

  it("falls back to node_id / empty app_session_id when the request bag omits them", () => {
    const e = diagnosticEvent({ kind: "approval", request: { prompt: "Proceed?" }, state: "pending" });
    expect(mapUserInteractionEventToRequest(e)).toMatchObject({
      request_id: "ui-node", app_session_id: "", status: "pending",
    });
  });

  it("returns false/null for a non-diagnostic or non-user_interaction event", () => {
    const output: WSEvent = { type: "output", data: { node_id: "x", output: "hi" } };
    expect(isActionableUserInteractionEvent(output)).toBe(false);
    expect(mapUserInteractionEventToRequest(output)).toBeNull();
  });
});

describe("instruction_widget mapping", () => {
  it("maps to a synthetic operator ChatMessage with the fixed sentinel id", () => {
    const n = node({
      node_id: "iw1", kind: "instruction_widget", ts: 42,
      payload: { text: "Read this before you start.", action: null },
    });
    const msg = mapInstructionWidgetToMessage(n);
    expect(msg.id).toBe(surfaceInstructionWidgetMessageId());
    expect(msg.role).toBe("operator");
    expect(msg.content).toBe("Read this before you start.");
    expect(msg.isStreaming).toBe(false);
  });

  it("surfaceInstructionWidgetMessageId is a stable, session-scoped (non-per-turn) constant", () => {
    expect(surfaceInstructionWidgetMessageId()).toBe(surfaceInstructionWidgetMessageId());
    expect(surfaceInstructionWidgetMessageId()).not.toContain(TURN);
  });
});

describe("upsertEventByNodeId", () => {
  it("appends when no existing event shares the node_id", () => {
    const events: WSEvent[] = [{ type: "output", data: { node_id: "a", output: "1" } }];
    const next = upsertEventByNodeId(events, { type: "output", data: { node_id: "b", output: "2" } });
    expect(next).toHaveLength(2);
  });

  it("replaces in place when the node_id matches", () => {
    const events: WSEvent[] = [
      { type: "output", data: { node_id: "a", output: "1" } },
      { type: "output", data: { node_id: "b", output: "2" } },
    ];
    const next = upsertEventByNodeId(events, { type: "output", data: { node_id: "a", output: "1-updated" } });
    expect(next).toHaveLength(2);
    expect(next[0].data.output).toBe("1-updated");
    expect(next[1].data.output).toBe("2");
  });

  it("always appends when the event carries no node_id", () => {
    const events: WSEvent[] = [{ type: "output", data: {} }];
    const next = upsertEventByNodeId(events, { type: "output", data: {} });
    expect(next).toHaveLength(2);
  });
});

describe("mapPromptToUserMessage", () => {
  it("splits attachments into images vs files and sets deterministic id", () => {
    const prompt = node({
      node_id: "prompt-uuid-1", kind: "typed_prompt",
      payload: {
        text: "hi", send_mode: "queue", origin: "user", source_session_ref: null,
        sent_text: null, intent_id: null,
        attachments: [
          { name: "pic.png", media_type: "image/png", ref: "ref1" },
          { name: "doc.pdf", media_type: "application/pdf", ref: "ref2" },
        ],
      },
    });
    const msg = mapPromptToUserMessage(prompt);
    expect(msg.id).toBe(surfaceUserMessageId(TURN));
    expect(msg.role).toBe("user");
    expect(msg.content).toBe("hi");
    expect(msg.images).toEqual([{ filename: "pic.png", media_type: "image/png" }]);
    expect(msg.files).toEqual([{ name: "doc.pdf", media_type: "application/pdf", size: 0 }]);
  });
});

describe("reduceWorkerInteractions", () => {
  function factNode(seq: number, factKind: string, fact: Record<string, unknown>): NodeWire {
    return node({ node_id: `w${seq}`, kind: "worker_interaction", seq, payload: { fact_kind: factKind, fact } });
  }

  it("builds a WorkerPanel from start -> event -> complete facts, in (ts,seq) order", () => {
    const nodes = [
      factNode(2, "worker_event", { delegation_id: "d1", event: { type: "output", data: { uuid: "e1", output: "hi" } } }),
      factNode(0, "worker_start", {
        delegation_id: "d1", worker_session_id: "w-sess", worker_description: "does stuff",
        is_new: true, instructions_preview: "preview",
      }),
      factNode(3, "worker_complete", { delegation_id: "d1", success: true, error: null }),
    ];
    const panels = reduceWorkerInteractions(nodes);
    expect(panels).toHaveLength(1);
    expect(panels[0].delegation_id).toBe("d1");
    expect(panels[0].worker_session_id).toBe("w-sess");
    expect(panels[0].events).toHaveLength(1);
    expect(panels[0].success).toBe(true);
  });

  it("ignores worker_event/worker_complete facts for an unknown delegation_id", () => {
    const nodes = [
      factNode(0, "worker_event", { delegation_id: "ghost", event: { type: "output", data: {} } }),
      factNode(1, "worker_complete", { delegation_id: "ghost", success: false }),
    ];
    expect(reduceWorkerInteractions(nodes)).toEqual([]);
  });

  it("ignores a duplicate worker_start for the same delegation_id", () => {
    const nodes = [
      factNode(0, "worker_start", { delegation_id: "d1", is_new: true, instructions_preview: "", worker_description: "a" }),
      factNode(1, "worker_start", { delegation_id: "d1", is_new: true, instructions_preview: "", worker_description: "b" }),
    ];
    const panels = reduceWorkerInteractions(nodes);
    expect(panels).toHaveLength(1);
    expect(panels[0].worker_description).toBe("a");
  });
});

describe("mapTurnToAssistantMessage / mapTurnToMessages", () => {
  function turn(overrides: Partial<CompactTurnWire> = {}, turnStatus: NodeWire["status"] = "streaming"): CompactTurnWire {
    const promptNode = node({ node_id: TURN, kind: "typed_prompt", payload: { text: "do it", attachments: [], send_mode: "queue", origin: "user", source_session_ref: null, sent_text: null, intent_id: null } });
    return {
      turn: node({ node_id: `turn:${TURN}`, kind: "turn", payload: null, status: turnStatus, ts: 500 }),
      prompt: promptNode,
      results: [],
      manifest: { renderable_child_count: 0, has_children: false },
      runtime_change: null,
      ...overrides,
    };
  }

  it("orders body nodes by (ts,seq) regardless of input order and flattens to events", () => {
    const body: NodeWire[] = [
      node({ node_id: "b2", kind: "assistant_text", ts: 20, seq: 0, payload: { text: "second" } }),
      node({ node_id: "b1", kind: "assistant_text", ts: 10, seq: 0, payload: { text: "first" } }),
    ];
    const msg = mapTurnToAssistantMessage(turn(), body);
    expect(msg.events.map((e) => e.data.output)).toEqual(["first", "second"]);
    expect(msg.content).toBe("first\nsecond");
  });

  it("is streaming when there is no result node and the turn is still streaming", () => {
    const msg = mapTurnToAssistantMessage(turn({}, "streaming"), []);
    expect(msg.isStreaming).toBe(true);
    expect(msg.completed_at).toBeUndefined();
  });

  it("finalizes (isStreaming false, completed_at set) once a RESULT node is present", () => {
    const resultNode = node({ node_id: "res1", kind: "result", payload: { result_kind: "provider" }, ts: 999 });
    const msg = mapTurnToAssistantMessage(turn({ results: [resultNode] }), []);
    expect(msg.isStreaming).toBe(false);
    expect(msg.completed_at).toBeDefined();
  });

  it("prefers the result's associated assistant_text over body text when both exist", () => {
    const resultNode = node({ node_id: "res1", kind: "result", payload: { result_kind: "provider" } });
    const resultText = node({ node_id: "res1-text", kind: "assistant_text", payload: { text: "final answer" } });
    const body = [node({ node_id: "b1", kind: "assistant_text", payload: { text: "draft" } })];
    const msg = mapTurnToAssistantMessage(turn({ results: [resultNode, resultText] }), body);
    expect(msg.content).toBe("final answer");
  });

  it("stamps error/errorText from a failure body node", () => {
    const body = [node({ node_id: "fail1", kind: "failure", payload: { code: "provider_error", text: "upstream 500", data: null } })];
    const msg = mapTurnToAssistantMessage(turn(), body);
    expect(msg.error).toBe(true);
    expect(msg.errorText).toBe("upstream 500");
  });

  it("stamps continuation_active from a continuation_session body node", () => {
    const body = [node({ node_id: "cont1", kind: "continuation_session", payload: { execution_ref: "exec1", chain_depth: 2, summary: null } })];
    const msg = mapTurnToAssistantMessage(turn(), body);
    expect(msg.continuation_active).toBe(2);
  });

  it("stamps run_meta from the first body node's run_ref, resolved against runsById", () => {
    const run: RunWire = { run_ref: "run-1", provider_id: "claude", account_name: null, model: "sonnet-4-6", reasoning_effort: "high", runner: "native" };
    const body = [node({ node_id: "b1", kind: "assistant_text", run_ref: "run-1", payload: { text: "hi" } })];
    const msg = mapTurnToAssistantMessage(turn(), body, new Map([["run-1", run]]));
    expect(msg.run_meta).toEqual({ provider_id: "claude", model: "sonnet-4-6", reasoning_effort: "high", runner: "native" });
  });

  it("maps turn.runtime_change (e.g. harness_change) into the assistant message's events", () => {
    const runtimeChange = node({
      node_id: "hc1", kind: "harness_change",
      payload: { from_harness_profile_id: null, to_harness_profile_id: "prof-2" },
    });
    const msg = mapTurnToAssistantMessage(turn({ runtime_change: runtimeChange }), []);
    expect(msg.events).toEqual([
      {
        type: "model_switched",
        data: {
          node_id: "hc1", changed: ["harness_profile"],
          harness_profile_id: "prof-2", previous_harness_profile_id: "",
        },
      },
    ]);
  });

  it("mapTurnToMessages returns [user, assistant] with deterministic ids", () => {
    const messages = mapTurnToMessages(turn(), []);
    expect(messages).toHaveLength(2);
    expect(messages[0].role).toBe("user");
    expect(messages[0].id).toBe(surfaceUserMessageId(TURN));
    expect(messages[1].role).toBe("assistant");
    expect(messages[1].id).toBe(surfaceAssistantMessageId(TURN));
  });

  it("mapTurnToMessages omits the user message when the turn has no prompt yet (live-discovered turn)", () => {
    const messages = mapTurnToMessages(turn({ prompt: null }), []);
    expect(messages).toHaveLength(1);
    expect(messages[0].role).toBe("assistant");
  });
});

describe("applyTextDeltaToNode / applyNodeStatusToNode", () => {
  it("appends text onto an assistant_text node's payload", () => {
    const n = node({ node_id: "a1", kind: "assistant_text", payload: { text: "hel" } });
    const next = applyTextDeltaToNode(n, "lo");
    expect((next.payload as { text: string }).text).toBe("hello");
  });

  it("appends text onto a thinking node's payload", () => {
    const n = node({ node_id: "t1", kind: "thinking", payload: { text: "pon", redacted: false } });
    const next = applyTextDeltaToNode(n, "der");
    expect((next.payload as { text: string }).text).toBe("ponder");
  });

  it("is a no-op for a node kind with no plain text payload field", () => {
    const n = node({ node_id: "tool:x", kind: "tool_interaction", payload: { tool_name: "Bash", args: {}, result: null, approval_ref: null, ui_kind: null, derived_view: null } });
    expect(applyTextDeltaToNode(n, "x")).toBe(n);
  });

  it("updates node status, no-op when unchanged", () => {
    const n = node({ node_id: "tool:x", kind: "tool_interaction", status: "streaming", payload: null });
    const next = applyNodeStatusToNode(n, "complete");
    expect(next.status).toBe("complete");
    expect(applyNodeStatusToNode(next, "complete")).toBe(next);
  });
});

describe("turnPhaseToMessagePatch", () => {
  const base: Pick<TurnLifecycleFrame, "phase" | "reason" | "usage"> = {
    phase: "running", reason: null, usage: null,
  };

  it("marks streaming while running/starting/queued", () => {
    expect(turnPhaseToMessagePatch({ ...base, phase: "running" })).toMatchObject({ isStreaming: true });
    expect(turnPhaseToMessagePatch({ ...base, phase: "starting" })).toMatchObject({ isStreaming: true });
  });

  it("marks terminal (isStreaming false) and stamps completed_at on completed", () => {
    const patch = turnPhaseToMessagePatch({ ...base, phase: "completed" });
    expect(patch.isStreaming).toBe(false);
    expect(patch.completed_at).toBeDefined();
  });

  it("marks stopped_at on stopped", () => {
    const patch = turnPhaseToMessagePatch({ ...base, phase: "stopped" });
    expect(patch.isStreaming).toBe(false);
    expect(patch.stopped_at).toBeDefined();
  });

  it("marks error/errorText on failed, using reason as the text", () => {
    const patch = turnPhaseToMessagePatch({ ...base, phase: "failed", reason: "provider_error" });
    expect(patch.error).toBe(true);
    expect(patch.errorText).toBe("provider_error");
  });

  it("stamps tokenUsage from a non-null usage field", () => {
    const patch = turnPhaseToMessagePatch({ ...base, usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 } });
    expect(patch.tokenUsage).toMatchObject({ input_tokens: 10, output_tokens: 5 });
  });
});
