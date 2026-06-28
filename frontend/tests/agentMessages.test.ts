import { describe, expect, it } from "vitest";
import {
  extractAssistantOutputTextFromEvents,
  extractAssistantTextFromEvents,
  flattenClaudeMessages,
} from "../src/utils/agentMessages";
import type { WSEvent } from "../src/types";

function assistantText(text: string, uuid: string): WSEvent {
  return {
    type: "agent_message",
    data: {
      type: "assistant",
      message: {
        role: "assistant",
        content: [{ type: "text", text }],
      },
      uuid,
    },
  };
}

function agentToolCall(toolUseId: string): WSEvent {
  return {
    type: "agent_message",
    data: {
      type: "assistant",
      message: {
        role: "assistant",
        content: [{
          type: "tool_use",
          id: toolUseId,
          name: "Agent",
          input: { subagent_type: "explorer", prompt: "check file" },
        }],
      },
      uuid: `${toolUseId}-event`,
    },
  };
}

function toolResult(toolUseId: string, content: string): WSEvent {
  return {
    type: "agent_message",
    data: {
      type: "user",
      message: {
        role: "user",
        content: [{ type: "tool_result", tool_use_id: toolUseId, content }],
      },
      uuid: `${toolUseId}-result`,
    },
  };
}

function thinkingText(text: string, uuid: string): WSEvent {
  return {
    type: "thinking",
    data: { thought: text },
    uuid,
  } as WSEvent;
}

describe("flattenClaudeMessages", () => {
  it("suppresses duplicated raw Codex native agent_message fallback", () => {
    const text = "Readable answer";
    const raw = [
      "Codex native event_msg.agent_message",
      "",
      "```json",
      JSON.stringify({ type: "agent_message", message: text }),
      "```",
    ].join("\n");

    const { flat } = flattenClaudeMessages([
      assistantText(raw, "raw-1"),
      assistantText(text, "normalized-1"),
    ]);

    expect(flat).toHaveLength(1);
    expect(flat[0].type).toBe("output");
    expect((flat[0].data as { output: string }).output).toBe(text);
  });

  it("keeps raw Codex native fallback when no normalized twin exists", () => {
    const raw = [
      "Codex native event_msg.turn_diff",
      "",
      "```json",
      JSON.stringify({ type: "turn_diff", message: "keep me" }),
      "```",
    ].join("\n");

    const { flat } = flattenClaudeMessages([assistantText(raw, "raw-1")]);

    expect(flat).toHaveLength(1);
    expect((flat[0].data as { output: string }).output).toContain(
      "Codex native event_msg.turn_diff",
    );
  });

  it("renders raw Codex native context_compacted fallback as a readable notice", () => {
    const raw = [
      "Codex native event_msg.context_compacted",
      "",
      "```json",
      JSON.stringify({ type: "context_compacted" }),
      "```",
    ].join("\n");

    const { flat } = flattenClaudeMessages([assistantText(raw, "raw-1")]);

    expect(flat).toHaveLength(1);
    expect(flat[0].type).toBe("lifecycle_notice");
    expect((flat[0].data as { message: string }).message).toBe("Context compacted");
    expect(extractAssistantTextFromEvents([assistantText(raw, "raw-1")])).toBe("");
  });

  it("unwraps backend lifecycle_notice events instead of rendering diagnostics", () => {
    const { flat } = flattenClaudeMessages([{
      type: "agent_message",
      data: {
        type: "lifecycle_notice",
        data: {
          kind: "context_compacted",
          message: "Context compacted",
        },
        uuid: "notice-1",
      },
    } as WSEvent]);

    expect(flat).toHaveLength(1);
    expect(flat[0].type).toBe("lifecycle_notice");
    expect((flat[0].data as { message: string }).message).toBe("Context compacted");
  });

  it("renders raw Codex native turn_aborted fallback as a readable notice", () => {
    const raw = [
      "Codex native event_msg.turn_aborted",
      "",
      "```json",
      JSON.stringify({
        type: "turn_aborted",
        turn_id: "019ed2bd-78f8-73f2-b63d-6499a5482626",
        reason: "interrupted",
        completed_at: 1781653400,
        duration_ms: 1307799,
      }),
      "```",
    ].join("\n");

    const { flat } = flattenClaudeMessages([assistantText(raw, "raw-1")]);

    expect(flat).toHaveLength(1);
    expect(flat[0].type).toBe("lifecycle_notice");
    expect((flat[0].data as { message: string }).message).toBe(
      "Turn interrupted after 21m 48s",
    );
    expect(extractAssistantTextFromEvents([assistantText(raw, "raw-1")])).toBe("");
  });

  it("attaches persisted Codex subagent notification results to the Agent call", () => {
    const { toolResultById } = flattenClaudeMessages([
      agentToolCall("call_agent"),
      toolResult("call_agent", JSON.stringify({ agent_id: "agent-1", nickname: "Turing" })),
      toolResult("agent-1", JSON.stringify({ completed: "Found backend/codex_native.py" })),
    ]);

    expect(toolResultById.get("call_agent")).toContain("Found backend/codex_native.py");
    expect(toolResultById.has("agent-1")).toBe(false);
  });

  it("surfaces orphaned Codex tool results in the flat chat event stream", () => {
    const { flat, toolResultById } = flattenClaudeMessages([
      toolResult("call_missing", "Chunk ID: 123\nOutput:\nvisible result"),
    ]);

    expect(toolResultById.get("call_missing")).toContain("visible result");
    expect(flat).toHaveLength(1);
    expect(flat[0].type).toBe("tool_result");
    expect((flat[0].data as { output: string; orphan_tool_result?: boolean }).output).toContain(
      "visible result",
    );
    expect((flat[0].data as { orphan_tool_result?: boolean }).orphan_tool_result).toBe(true);
  });

  it("keeps matched tool results in flat chat events without marking them orphaned", () => {
    const { flat, toolResultById } = flattenClaudeMessages([
      {
        type: "agent_message",
        data: {
          type: "assistant",
          message: {
            role: "assistant",
            content: [{
              type: "tool_use",
              id: "call_exec",
              name: "Bash",
              input: { command: "echo visible" },
            }],
          },
          uuid: "call_exec-event",
        },
      } as WSEvent,
      toolResult("call_exec", "visible result"),
    ]);

    expect(toolResultById.get("call_exec")).toBe("visible result");
    expect(flat).toHaveLength(2);
    expect(flat[0].type).toBe("tool_call");
    expect(flat[1].type).toBe("tool_result");
    expect((flat[1].data as { output: string; paired_tool_result?: boolean }).output).toBe("visible result");
    expect((flat[1].data as { paired_tool_result?: boolean }).paired_tool_result).toBe(true);
  });

  it("extracts assistant speech from output events only", () => {
    const raw = [
      "Codex native event_msg.context_compacted",
      "",
      "```json",
      JSON.stringify({ type: "context_compacted" }),
      "```",
    ].join("\n");

    expect(extractAssistantOutputTextFromEvents([
      thinkingText("private reasoning", "thinking-1"),
      assistantText("Final answer", "answer-1"),
      assistantText(raw, "notice-1"),
    ])).toBe("Final answer");
  });
});
