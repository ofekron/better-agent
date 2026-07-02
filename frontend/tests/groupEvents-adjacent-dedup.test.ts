import { describe, expect, it } from "vitest";
import { groupEvents } from "../src/components/MessageBubble";
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
    data: { tool_use_id: id, tool: "Bash", args: { command: "ls" } },
  };
}

describe("groupEvents adjacent-twin dedup", () => {
  it("keeps legitimately repeated text separated by a tool call", () => {
    const events = [output("X"), toolCall("t1"), output("X")];
    const groups = groupEvents(events, new Map([["t1", "ok"]]));

    const textRows = groups.filter(
      (g) => g.kind === "event" && g.event.type === "output",
    );
    expect(textRows).toHaveLength(2);
    expect(groups.filter((g) => g.kind === "tool")).toHaveLength(1);
  });

  it("dedups a thinking/output twin pair with identical adjacent text", () => {
    const groups = groupEvents([thinking("X"), output("X")]);

    expect(groups).toHaveLength(1);
    expect(groups[0].kind).toBe("event");
    expect(groups[0].event.type).toBe("thinking");
  });

  it("dedups adjacent identical outputs", () => {
    const groups = groupEvents([output("X"), output("X")]);
    expect(groups).toHaveLength(1);
  });

  it("keeps repeated text separated by different text", () => {
    const groups = groupEvents([output("X"), output("Y"), output("X")]);
    expect(groups).toHaveLength(3);
  });
});
