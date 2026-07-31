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

describe("toolResultContentToString (exercised via tool_result content)", () => {
  function userToolResult(content: unknown): WSEvent {
    return {
      type: "agent_message",
      data: {
        type: "user",
        message: { role: "user", content: [{ type: "tool_result", tool_use_id: "tr", content }] },
        uuid: "tr-ev",
      },
    } as WSEvent;
  }
  function resultOutput(content: unknown): string {
    const { flat } = flattenClaudeMessages([userToolResult(content)]);
    return (flat.find((e) => e.type === "tool_result")!.data as { output: string }).output;
  }

  it("returns empty string for null/undefined content", () => {
    expect(resultOutput(undefined)).toBe("");
    expect(resultOutput(null)).toBe("");
  });

  it("passes a plain string content through verbatim", () => {
    const { flat } = flattenClaudeMessages([userToolResult("plain string")]);
    expect((flat.at(-1)!.data as { output: string }).output).toBe("plain string");
  });

  it("joins text blocks of an array with newlines and JSON-serializes non-text objects", () => {
    const content = [
      { type: "text", text: "first" },
      { type: "image" },
      { type: "text", text: "second" },
    ];
    const { flat } = flattenClaudeMessages([userToolResult(content)]);
    const out = (flat.at(-1)!.data as { output: string }).output;
    expect(out).toContain("first");
    expect(out).toContain("second");
    expect(out).toContain('"type":"image"');
  });

  it("keeps raw string elements inside an array content", () => {
    const content = ["alpha", { type: "text", text: "beta" }];
    const { flat } = flattenClaudeMessages([userToolResult(content)]);
    expect((flat.at(-1)!.data as { output: string }).output).toBe("alpha\nbeta");
  });

  it("falls back to String() when a content block is circular", () => {
    const circular: Record<string, unknown> = { type: "loop" };
    circular.self = circular;
    const { flat } = flattenClaudeMessages([userToolResult([circular])]);
    expect((flat.at(-1)!.data as { output: string }).output).toBe("[object Object]");
  });

  it("JSON-serializes a non-array object content and String()-falls-back on circular", () => {
    const obj = { ok: true, n: 3 };
    const a = flattenClaudeMessages([userToolResult(obj)]);
    expect((a.flat.at(-1)!.data as { output: string }).output).toBe(JSON.stringify(obj));

    const circular: Record<string, unknown> = {};
    circular.self = circular;
    const b = flattenClaudeMessages([userToolResult(circular)]);
    expect((b.flat.at(-1)!.data as { output: string }).output).toBe("[object Object]");
  });
});

describe("normalizeToolName", () => {
  it("maps mcp__<server>__delegate tool names to 'delegate'", () => {
    const ev: WSEvent = {
      type: "agent_message",
      data: {
        type: "assistant",
        message: {
          role: "assistant",
          content: [{ type: "tool_use", id: "d1", name: "mcp__my_server__delegate", input: { task: "x" } }],
        },
        uuid: "delegate-ev",
      },
    } as WSEvent;
    const { flat } = flattenClaudeMessages([ev]);
    expect(flat[0].type).toBe("tool_call");
    expect((flat[0].data as { tool: string }).tool).toBe("delegate");
  });
});

describe("durationText (via Codex turn_aborted notice)", () => {
  function abortedRaw(durationMs: unknown, reason = "interrupted"): string {
    return [
      "Codex native event_msg.turn_aborted",
      "",
      "```json",
      JSON.stringify({ type: "turn_aborted", reason, duration_ms: durationMs }),
      "```",
    ].join("\n");
  }
  function noticeMessage(durationMs: unknown): string {
    const { flat } = flattenClaudeMessages([assistantText(abortedRaw(durationMs), "r")]);
    return (flat[0].data as { message: string }).message;
  }

  it("omits the duration suffix when duration_ms is below one second or non-integer", () => {
    expect(noticeMessage(500)).toBe("Turn interrupted");
    expect(noticeMessage(999.5)).toBe("Turn interrupted");
    expect(noticeMessage("900")).toBe("Turn interrupted");
  });

  it("formats a seconds-only duration when under a minute", () => {
    expect(noticeMessage(45_000)).toBe("Turn interrupted after 45s");
  });

  it("formats an hours+minutes+seconds duration", () => {
    // 1h 1m 40s
    expect(noticeMessage(3_700_000)).toBe("Turn interrupted after 1h 1m 40s");
  });

  it("uses 'Turn aborted' for a non-interrupted reason", () => {
    expect(noticeMessage(45_000)).toBe("Turn interrupted after 45s");
    const { flat } = flattenClaudeMessages([assistantText(abortedRaw(45_000, "other"), "r")]);
    expect((flat[0].data as { message: string }).message).toBe("Turn aborted after 45s");
  });
});

describe("codexNativeFallbackLifecycleNotice malformed payload", () => {
  it("falls back to a rendered output when the JSON body is unparseable", () => {
    const raw = [
      "Codex native event_msg.turn_aborted",
      "",
      "```json",
      "{not valid json}",
      "```",
    ].join("\n");
    const { flat } = flattenClaudeMessages([assistantText(raw, "bad-1")]);
    expect(flat).toHaveLength(1);
    expect(flat[0].type).toBe("output");
  });
});

describe("deriveTs timestamp sources", () => {
  it("prefers data.timestamp, then data.message.timestamp", () => {
    const a = flattenClaudeMessages([
      { type: "output", data: { timestamp: "T-DATA", output: "a" } } as WSEvent,
    ]);
    expect(a.flat[0]._ts).toBe("T-DATA");

    const b = flattenClaudeMessages([
      { type: "output", data: { message: { timestamp: "T-MSG" }, output: "b" } } as WSEvent,
    ]);
    expect(b.flat[0]._ts).toBe("T-MSG");

    const c = flattenClaudeMessages([
      { type: "output", data: { output: "c" } } as WSEvent,
    ]);
    expect(c.flat[0]._ts).toBeUndefined();
  });
});

describe("diagnostic emission and envelope dropping", () => {
  it("emits a diagnostic for an unknown agent_message data.type", () => {
    const { flat } = flattenClaudeMessages([{
      type: "agent_message",
      data: { type: "weird_type", foo: 1 },
      uuid: "weird-1",
    } as WSEvent]);
    expect(flat).toHaveLength(1);
    expect(flat[0].type).toBe("diagnostic");
    expect((flat[0].data as { kind: string }).kind).toBe("agent_message.weird_type");
  });

  it("drops raw provider protocol envelopes that leaked through un-normalized", () => {
    const dropped = ["response_item", "event_msg", "session_meta", "turn_context", "compacted", "thread.started"];
    const events: WSEvent[] = dropped.map((t, i) => ({
      type: "agent_message",
      data: { type: t },
      uuid: `d-${i}`,
    }) as WSEvent);
    const { flat } = flattenClaudeMessages(events);
    expect(flat).toHaveLength(0);
  });

  it("emits a diagnostic for an unknown assistant content block type", () => {
    const ev: WSEvent = {
      type: "agent_message",
      data: {
        type: "assistant",
        message: { role: "assistant", content: [{ type: "mystery", payload: 1 }] },
        uuid: "myst-1",
      },
    } as WSEvent;
    const { flat } = flattenClaudeMessages([ev]);
    expect(flat).toHaveLength(1);
    expect(flat[0].type).toBe("diagnostic");
    expect((flat[0].data as { kind: string }).kind).toBe("block.mystery");
  });

  it("emits a diagnostic with '(none)' when the block type is absent", () => {
    const ev: WSEvent = {
      type: "agent_message",
      data: {
        type: "assistant",
        message: { role: "assistant", content: [{ payload: 1 }] },
        uuid: "none-1",
      },
    } as WSEvent;
    const { flat } = flattenClaudeMessages([ev]);
    expect((flat[0].data as { kind: string }).kind).toBe("block.(none)");
  });
});

describe("assistant-content block validation", () => {
  function assistantWith(content: unknown[]): WSEvent {
    return {
      type: "agent_message",
      data: {
        type: "assistant",
        message: { role: "assistant", content },
        uuid: "a-1",
      },
    } as WSEvent;
  }

  it("skips text blocks whose text is not a string", () => {
    const { flat } = flattenClaudeMessages([
      assistantWith([{ type: "text", text: 123 }, { type: "text", text: "kept" }]),
    ]);
    expect(flat).toHaveLength(1);
    expect((flat[0].data as { output: string }).output).toBe("kept");
  });

  it("skips thinking blocks whose thinking is not a string", () => {
    const { flat } = flattenClaudeMessages([
      assistantWith([{ type: "thinking", thinking: 123 }, { type: "thinking", thinking: "kept" }]),
    ]);
    expect(flat).toHaveLength(1);
    expect((flat[0].data as { thought: string }).thought).toBe("kept");
  });

  it("skips tool_use blocks whose name is not a string", () => {
    const { flat } = flattenClaudeMessages([
      assistantWith([{ type: "tool_use", id: "x", name: 123 }, { type: "tool_use", id: "y", name: "Bash" }]),
    ]);
    expect(flat).toHaveLength(1);
    expect((flat[0].data as { tool: string }).tool).toBe("Bash");
  });

  it("skips non-object elements inside the assistant content array", () => {
    const { flat } = flattenClaudeMessages([
      assistantWith([null, "stray", { type: "text", text: "kept" } as unknown]),
    ]);
    expect(flat).toHaveLength(1);
    expect((flat[0].data as { output: string }).output).toBe("kept");
  });

  it("renders a tool_result block embedded in assistant content and records its text", () => {
    const { flat, toolResultById } = flattenClaudeMessages([
      assistantWith([{ type: "tool_result", tool_use_id: "in-asst", content: "embedded" }]),
    ]);
    expect(toolResultById.get("in-asst")).toBe("embedded");
    const tr = flat.find((e) => e.type === "tool_result")!;
    expect(tr).toBeTruthy();
    expect((tr.data as { output: string; orphan_tool_result: boolean }).output).toBe("embedded");
    expect((tr.data as { orphan_tool_result: boolean }).orphan_tool_result).toBe(true);
  });

  it("skips assistant tool_result blocks without a string tool_use_id", () => {
    const { flat } = flattenClaudeMessages([
      assistantWith([{ type: "tool_result", content: "no-id" }, { type: "text", text: "kept" }]),
    ]);
    expect(flat.find((e) => e.type === "tool_result")).toBeUndefined();
    expect(flat).toHaveLength(1);
    expect((flat[0].data as { output: string }).output).toBe("kept");
  });
});

describe("extractAssistantTextFromEvents empty-string fallback", () => {
  it("drops output events whose first-stringable field is empty", () => {
    const ev: WSEvent = {
      type: "agent_message",
      data: {
        type: "assistant",
        message: { role: "assistant", content: [{ type: "text", text: "" }] },
        uuid: "empty-1",
      },
    } as WSEvent;
    // No non-empty output/thinking fields → firstString returns "" and the entry is filtered.
    expect(extractAssistantTextFromEvents([ev])).toBe("");
    expect(extractAssistantOutputTextFromEvents([ev])).toBe("");
  });

  it("returns '' when pre-flattened output/thinking events carry no stringable field", () => {
    // Non-agent_message events pass through flatten unchanged; with no
    // output/thought field, firstString yields "" and they are filtered out.
    expect(
      extractAssistantTextFromEvents([
        { type: "output", data: {} } as WSEvent,
        { type: "thinking", data: {} } as WSEvent,
      ]),
    ).toBe("");
  });

  it("respects the limit argument", () => {
    const ev = assistantText("x".repeat(100), "big-1");
    expect(extractAssistantTextFromEvents([ev], 10)).toHaveLength(10);
  });
});

describe("internal helper edge branches", () => {
  it("treats a non-JSON Agent tool_result as having no agent_id (no remap)", () => {
    // jsonObjectFromString catches invalid JSON → returns null → no agent_id mapping.
    const { toolResultById } = flattenClaudeMessages([
      agentToolCall("call_agent"),
      toolResult("call_agent", "not valid json"),
      toolResult("agent-1", "should not be remapped onto call_agent"),
    ]);
    // call_agent keeps its own (non-JSON) result; agent-1 is NOT remapped away.
    expect(toolResultById.get("call_agent")).toBe("not valid json");
    expect(toolResultById.get("agent-1")).toBe("should not be remapped onto call_agent");
  });

  it("treats a malformed Codex agent_message fallback as non-duplicate", () => {
    // duplicateCodexNativeAgentMessage catches invalid JSON → returns false → not suppressed.
    const raw = [
      "Codex native event_msg.agent_message",
      "",
      "```json",
      "{bad json}",
      "```",
    ].join("\n");
    const { flat } = flattenClaudeMessages([assistantText(raw, "bad-dup")]);
    // Not a recognized lifecycle notice either → rendered as output.
    expect(flat).toHaveLength(1);
    expect(flat[0].type).toBe("output");
  });

  it("drops agent_message envelopes of internal bookkeeping types", () => {
    const dropped = ["system", "queue-operation", "last-prompt", "attachment", "ai-title", "file-history-snapshot", "file-history-delta", "mode"];
    const events: WSEvent[] = dropped.map((t, i) => ({
      type: "agent_message",
      data: { type: t },
      uuid: `bk-${i}`,
    }) as WSEvent);
    const { flat } = flattenClaudeMessages(events);
    expect(flat).toHaveLength(0);
  });
});

describe("fallback content block", () => {
  it("maps a fallback block to a model_fallback event, not a diagnostic", () => {
    const ev: WSEvent = {
      type: "agent_message",
      data: {
        type: "assistant",
        message: {
          role: "assistant",
          content: [{
            type: "fallback",
            from: { model: "claude-fable-5" },
            to: { model: "claude-opus-4-8" },
          }],
        },
        uuid: "fallback-1",
      },
    };
    const { flat } = flattenClaudeMessages([ev]);
    expect(flat).toEqual([
      {
        type: "model_fallback",
        data: { from_model: "claude-fable-5", to_model: "claude-opus-4-8" },
        _ts: undefined,
      },
    ]);
  });

  it("defaults missing from/to models to empty strings", () => {
    const ev: WSEvent = {
      type: "agent_message",
      data: {
        type: "assistant",
        message: {
          role: "assistant",
          content: [{ type: "fallback", from: {}, to: { model: 7 } }],
        },
        uuid: "fallback-2",
      },
    } as WSEvent;
    const { flat } = flattenClaudeMessages([ev]);
    expect(flat[0].data).toEqual({ from_model: "", to_model: "" });
  });

  it("skips an assistant message whose content is not an array", () => {
    const ev: WSEvent = {
      type: "agent_message",
      data: { type: "assistant", message: { role: "assistant", content: "nope" } },
      uuid: "nocontent-1",
    } as WSEvent;
    const { flat } = flattenClaudeMessages([ev]);
    expect(flat).toHaveLength(0);
  });

  it("stamps _ts onto a Codex fallback notice when the event carries a timestamp", () => {
    const raw = [
      "Codex native event_msg.context_compacted",
      "",
      "```json",
      JSON.stringify({ type: "context_compacted" }),
      "```",
    ].join("\n");
    const ev: WSEvent = {
      type: "agent_message",
      data: {
        type: "assistant",
        message: { role: "assistant", content: [{ type: "text", text: raw }] },
        timestamp: "T-NOTICE",
        uuid: "ts-notice",
      },
    } as WSEvent;
    const { flat } = flattenClaudeMessages([ev]);
    expect(flat[0].type).toBe("lifecycle_notice");
    expect(flat[0]._ts).toBe("T-NOTICE");
  });
});
