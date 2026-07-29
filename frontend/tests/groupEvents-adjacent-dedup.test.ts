import { describe, expect, it } from "vitest";
import { groupEvents } from "../src/lib/groupEvents";
import type { WSEvent } from "../src/types";

function output(text: string): WSEvent {
  return { type: "output", data: { output: text } };
}

function thinking(text: string): WSEvent {
  return { type: "thinking", data: { thought: text } };
}

function toolCall(id: string): WSEvent {
  return {
    type: "tool_call",
    data: { tool_use_id: id, tool: "WebSearch", args: { query: "docs" } },
  };
}

describe("groupEvents adjacent-twin dedup", () => {
  it("keeps legitimately repeated text separated by a tool call", () => {
    const groups = groupEvents([output("X"), toolCall("t1"), output("X")]);
    expect(groups.filter((group) => group.kind === "event")).toHaveLength(2);
  });

  it("dedups a thinking/output twin pair with identical adjacent text", () => {
    expect(groupEvents([thinking("X"), output("X")])).toHaveLength(1);
  });

  it("dedups adjacent identical outputs", () => {
    expect(groupEvents([output("X"), output("X")])).toHaveLength(1);
  });

  it("keeps repeated text separated by different text", () => {
    expect(groupEvents([output("X"), output("Y"), output("X")])).toHaveLength(3);
  });
});
