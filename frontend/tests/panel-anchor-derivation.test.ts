import { describe, it, expect } from "vitest";
import { tagEvents } from "../src/utils/mergeEvents";
import type { WSEvent, WorkerPanel } from "../src/types";

const assistantToolUse = (...tools: [string, string][]): WSEvent => ({
  type: "agent_message",
  data: {
    type: "assistant",
    message: {
      content: tools.map(([name, id]) => ({ type: "tool_use", name, id, input: {} })),
    },
  },
});

const text = (label: string): WSEvent => ({
  type: "agent_message",
  data: { type: "assistant", message: { content: [{ type: "text", text: label }] } },
});

const toolResult = (label: string, toolUseId?: string): WSEvent => ({
  type: "agent_message",
  data: {
    type: "user",
    message: {
      content: [
        {
          type: "tool_result",
          content: label,
          ...(toolUseId ? { tool_use_id: toolUseId } : {}),
        },
      ],
    },
  },
});

const labelOf = (e: WSEvent): string => {
  const data = e.data as { message?: { content?: unknown[] } } | undefined;
  const content = data?.message?.content?.[0];
  if (!content || typeof content !== "object") return "?";
  const block = content as { text?: unknown; content?: unknown };
  if (typeof block.text === "string") return block.text;
  if (typeof block.content === "string") return block.content;
  return "?";
};

/** Flatten tagged stream to a comparable token list. */
const tokens = (managerEvents: WSEvent[], workers: WorkerPanel[]): string[] =>
  tagEvents(managerEvents, workers).map((t) =>
    t.entityType === "worker" ? `W:${labelOf(t.event)}` : labelOf(t.event),
  );

const panel = (p: Partial<WorkerPanel> & { delegation_id: string }): WorkerPanel => ({
  worker_session_id: "",
  worker_description: "",
  is_new: false,
  instructions_preview: "",
  events: [],
  ...p,
});

describe("tagEvents render-time anchor derivation", () => {
  it("places the sub-session group AFTER the ask, not before create_sub_session", () => {
    const managerEvents = [
      text("creating"),
      assistantToolUse(["mcp__handoff__create_sub_session", "c1"]),
      toolResult("created ok"),
      assistantToolUse(["mcp__communicate__ask", "a1"]),
      toolResult("ask ok"),
    ];
    const workers = [
      // racy/wrong stored insert_at=0 would put both at the very front
      panel({ delegation_id: "created_sub", panel_kind: "sub_session_created", run_mode: "created", insert_at: 0 }),
      panel({
        delegation_id: "team_ask_1",
        panel_kind: "sub_session",
        run_mode: "team_ask",
        insert_at: 0,
        events: [text("SUBAGENT_WORK")],
      }),
    ];
    expect(tokens(managerEvents, workers)).toEqual([
      "creating",
      "?", // create_sub_session tool_use entry
      "W:?", // "Sub Session Created" marker placeholder, right after create
      "created ok",
      "?", // ask tool_use entry
      "W:SUBAGENT_WORK", // sub-session group, AFTER the ask
      "ask ok",
    ]);
  });

  it("keeps parallel asks sharing one entry in firing order", () => {
    const managerEvents = [
      assistantToolUse(["mcp__communicate__ask", "a1"], ["mcp__communicate__ask", "a2"]),
    ];
    const workers = [
      panel({ delegation_id: "ask_1", panel_kind: "sub_session", run_mode: "team_ask", insert_at: 0, events: [text("ASK_ONE")] }),
      panel({ delegation_id: "ask_2", panel_kind: "sub_session", run_mode: "team_ask", insert_at: 0, events: [text("ASK_TWO")] }),
    ];
    expect(tokens(managerEvents, workers)).toEqual(["?", "W:ASK_ONE", "W:ASK_TWO"]);
  });

  it("ignores create_worker tool_use so it does not desync a later ask", () => {
    const managerEvents = [
      assistantToolUse(["mcp__communicate__create_worker", "w1"]),
      assistantToolUse(["mcp__communicate__ask", "a1"]),
    ];
    const workers = [
      panel({ delegation_id: "team_ask_1", panel_kind: "sub_session", run_mode: "team_ask", insert_at: 0, events: [text("SUBAGENT_WORK")] }),
    ];
    // anchors after the ask entry (index 1), not the create_worker entry (0)
    expect(tokens(managerEvents, workers)).toEqual(["?", "?", "W:SUBAGENT_WORK"]);
  });

  it("does not let a failed create_session consume a later success panel", () => {
    const managerEvents = [
      assistantToolUse(["mcp__communicate__create_session", "c1"]),
      toolResult("create_session failed: HTTP 400: provider_id does not exist", "c1"),
      assistantToolUse(["mcp__communicate__create_session", "c2"]),
      toolResult('{"success": true, "session_id": "s2", "name": "created"}', "c2"),
    ];
    const workers = [
      panel({
        delegation_id: "created_s2",
        worker_session_id: "s2",
        panel_kind: "session_created",
        run_mode: "created",
        insert_at: 0,
        events: [text("CREATED_PANEL")],
      }),
    ];
    expect(tokens(managerEvents, workers)).toEqual([
      "?",
      "create_session failed: HTTP 400: provider_id does not exist",
      "?",
      "W:CREATED_PANEL",
      '{"success": true, "session_id": "s2", "name": "created"}',
    ]);
  });

  it("falls back to stored insert_at for a panel with no matching tool_use", () => {
    const managerEvents = [text("a"), text("b"), text("c")];
    const workers = [
      panel({ delegation_id: "native_1", panel_kind: "worker", run_mode: "native", insert_at: 2, events: [text("NATIVE_WORK")] }),
    ];
    expect(tokens(managerEvents, workers)).toEqual(["a", "b", "W:NATIVE_WORK", "c"]);
  });
});
