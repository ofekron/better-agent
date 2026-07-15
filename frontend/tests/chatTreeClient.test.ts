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
      body: [{ type: "Explanation", text: "", text_event_ids: [], item_ids: ["e-tool"] }],
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
                data: { tool_name: "Bash", tool_use_id: "t1", status: "complete" },
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
