import { describe, expect, it } from "vitest";
import { chatTreeToMessages, type ChatTreeLookupEntry } from "src/chat/chatTreeClient";
import { parseProjection } from "src/chat/parseProjection";

/**
 * The transport adapter: structure and result resolution come from the
 * backend-projected formal tree (never rederived client-side); content,
 * seqs, and message state come from the lookup sidecar. ModelChange
 * items become model_switched boundary events on the preceding
 * assistant message, which the existing Chat renders heading the
 * affected turn.
 */
describe("chatTreeToMessages", () => {
  const items = [
    {
      type: "Turn", id: "u1", prompt: "u1",
      body: [{ type: "Explanation", text: "", text_event_ids: [], item_ids: ["e-tool", "e-result"] }],
      result: { type: "ProviderResult", part_ids: ["e-final"], text: "All done." },
    },
    { type: "ModelChange", id: "e-switch", before_turn: "u2" },
    {
      type: "Turn", id: "u2", prompt: "u2",
      body: [],
      result: { type: "DerivedResult", part_ids: ["e-t2"], text: "Second answer." },
    },
  ];
  const lookup: Record<string, ChatTreeLookupEntry> = {
    u1: { kind: "message", role: "user", text: "first prompt", seq: 10,
          snapshot: { id: "u1", role: "user", seq: 10, timestamp: "t1" } },
    u2: { kind: "message", role: "user", text: "second prompt", seq: 30,
          snapshot: { id: "u2", role: "user", seq: 30 } },
    a1: { kind: "message", role: "assistant", text: "", seq: 11,
          snapshot: { id: "a1", role: "assistant", seq: 11, isStreaming: false,
                      run_meta: { provider_id: "claude", model: "sonnet", reasoning_effort: "high" } } },
    a2: { kind: "message", role: "assistant", text: "", seq: 31,
          snapshot: { id: "a2", role: "assistant", seq: 31 } },
    "e-final": { kind: "event", type: "assistant_text", data: { text: "All done." },
                 message_id: "a1", message_seq: 11 },
    "e-tool": { kind: "event", type: "tool_interaction",
                data: { tool_name: "Bash", tool_use_id: "t1", status: "running" },
                message_id: "a1", message_seq: 11 },
    "e-result": { kind: "event", type: "tool_interaction",
                data: { tool_name: "Bash", tool_use_id: "t1", status: "complete", output: "ok" },
                message_id: "a1", message_seq: 11 },
    "e-t2": { kind: "event", type: "assistant_text", data: { text: "Second answer." },
              message_id: "a2", message_seq: 31 },
    "e-switch": { kind: "event", type: "model_change",
                  data: { from: { provider: "claude", model: "sonnet", effort: "high" },
                          to: { provider: "codex", model: "gpt-5-codex", effort: "high" } } },
  };

  it("builds the message list from tree structure + lookup content", () => {
    const messages = chatTreeToMessages(parseProjection(items), lookup);
    expect(messages.map((m) => [m.id, m.role, m.content, m.seq])).toEqual([
      ["u1", "user", "first prompt", 10],
      ["a1", "assistant", "All done.", 11],
      ["u2", "user", "second prompt", 30],
      ["a2", "assistant", "Second answer.", 31],
    ]);
    expect(messages[1].run_meta).toEqual(
      { provider_id: "claude", model: "sonnet", reasoning_effort: "high" });
  });

  it("preserves body and result events for cold REST rendering", () => {
    const messages = chatTreeToMessages(parseProjection(items), lookup);
    expect(messages[1].events?.map((event) => [event.type, event.data])).toEqual([
      ["tool_call", {
        uuid: "e-tool",
        tool_use_id: "t1",
        tool: "Bash",
        args: null,
      }],
      ["tool_result", {
        uuid: "e-result",
        tool_use_id: "t1",
        output: "ok",
      }],
      ["output", { uuid: "e-final", output: "All done." }],
      ["model_switched", {
        uuid: "e-switch",
        previous_provider_id: "claude",
        previous_model: "sonnet",
        previous_reasoning_effort: "high",
        provider_id: "codex",
        model: "gpt-5-codex",
        reasoning_effort: "high",
        changed: ["provider_id", "model"],
      }],
    ]);
  });

  it("attaches model changes as boundary events on the preceding assistant message", () => {
    const messages = chatTreeToMessages(parseProjection(items), lookup);
    const boundary = (messages[1].events ?? []).find((e) => e.type === "model_switched");
    expect(boundary?.data).toMatchObject({
      uuid: "e-switch",
      previous_provider_id: "claude",
      provider_id: "codex",
      model: "gpt-5-codex",
      changed: ["provider_id", "model"],
    });
    expect(messages[3].events?.map((event) => event.type)).toEqual(["output"]);
  });

  it("omits the assistant message when the turn has no owned events", () => {
    const promptOnly = [{ type: "Turn", id: "u9", prompt: "u9", body: [], result: null }];
    const messages = chatTreeToMessages(parseProjection(promptOnly), {
      u9: { kind: "message", role: "user", text: "unanswered", seq: 5, snapshot: { id: "u9" } },
    });
    expect(messages.map((m) => m.id)).toEqual(["u9"]);
  });
});

/**
 * Bug fix: a native subagent call (Claude Agent/Task tool) rendered its
 * sidechain events as flat top-level siblings of the parent turn instead
 * of nested under a NativeSubagentTurn. The backend fix stamps
 * parent_event_id so the tree carries a real NativeSubagentTurn body
 * item; this locks the frontend adapter side — the assistant message
 * must not be dropped when its entire body is a scoped turn, and nested
 * events must be flattened with `parent_tool_use_id` set to the scope's
 * id so the existing SubAgentBlock/partitionEventsByParent UI
 * (MessageBubble.tsx) picks them up and nests them, recursively.
 */
describe("chatTreeToMessages: NativeSubagentTurn nesting", () => {
  const items = [
    {
      type: "Turn", id: "u1", prompt: "u1",
      body: [{
        type: "NativeSubagentTurn", id: "scope-1", prompt: "Explore the codebase for X",
        body: [{ type: "Explanation", text: "", text_event_ids: [], item_ids: ["nested-tool"] }],
        result: { type: "ProviderResult", part_ids: ["nested-final"], text: "X is at line 10." },
        children: ["nested-tool", "nested-final"],
      }],
      result: null,
    },
  ];
  const lookup: Record<string, ChatTreeLookupEntry> = {
    u1: { kind: "message", role: "user", text: "find it", seq: 10,
          snapshot: { id: "u1", role: "user", seq: 10 } },
    a1: { kind: "message", role: "assistant", text: "", seq: 11,
          snapshot: { id: "a1", role: "assistant", seq: 11 } },
    "scope-1": { kind: "event", type: "native_subagent_turn",
                 data: { prompt: "Explore the codebase for X" }, message_id: "a1" },
    "nested-tool": { kind: "event", type: "tool_interaction",
                     data: { tool_name: "Read", tool_use_id: "tu-nested", status: "complete", output: "ok" },
                     message_id: "a1" },
    "nested-final": { kind: "event", type: "assistant_text",
                       data: { text: "X is at line 10." }, message_id: "a1" },
  };

  it("does not drop the assistant message when its whole body is a scoped turn", () => {
    const messages = chatTreeToMessages(parseProjection(items), lookup);
    expect(messages.map((m) => m.id)).toEqual(["u1", "a1"]);
  });

  it("synthesizes the scope as a tool_call keyed by its own id (reuses SubAgentBlock)", () => {
    const messages = chatTreeToMessages(parseProjection(items), lookup);
    const scopeEvent = messages[1].events?.find((e) => e.data.uuid === "scope-1");
    expect(scopeEvent).toMatchObject({
      type: "tool_call",
      data: { tool_use_id: "scope-1", tool: "Agent", args: { prompt: "Explore the codebase for X" } },
    });
  });

  it("stamps nested events with parent_tool_use_id pointing at the scope", () => {
    const messages = chatTreeToMessages(parseProjection(items), lookup);
    const nestedTool = messages[1].events?.find((e) => e.data.uuid === "nested-tool");
    const nestedFinal = messages[1].events?.find((e) => e.data.uuid === "nested-final");
    expect(nestedTool?.data.parent_tool_use_id).toBe("scope-1");
    expect(nestedFinal?.data.parent_tool_use_id).toBe("scope-1");
  });

  it("does not stamp parent_tool_use_id on top-level (unscoped) events", () => {
    const flat = [{
      type: "Turn", id: "u2", prompt: "u2",
      body: [{ type: "Explanation", text: "", text_event_ids: [], item_ids: ["plain-tool"] }],
      result: { type: "DerivedResult", part_ids: ["plain-final"], text: "Done." },
    }];
    const flatLookup: Record<string, ChatTreeLookupEntry> = {
      u2: { kind: "message", role: "user", text: "go", seq: 1, snapshot: { id: "u2" } },
      a2: { kind: "message", role: "assistant", text: "", seq: 2, snapshot: { id: "a2" } },
      "plain-tool": { kind: "event", type: "tool_interaction",
                      data: { tool_name: "Bash", tool_use_id: "t1", status: "complete", output: "ok" },
                      message_id: "a2" },
      "plain-final": { kind: "event", type: "assistant_text", data: { text: "Done." }, message_id: "a2" },
    };
    const messages = chatTreeToMessages(parseProjection(flat), flatLookup);
    for (const event of messages[1].events ?? []) {
      expect(event.data.parent_tool_use_id).toBeUndefined();
    }
  });
});
