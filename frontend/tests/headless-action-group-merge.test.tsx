import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import React from "react";
import { MessageBubble } from "../src/components/MessageBubble";
import { makeAssistantMsg } from "./fixtures";
import type { WSEvent } from "../src/types";

function agentMsg(data: Record<string, unknown>): WSEvent {
  return { type: "agent_message", data } as WSEvent;
}

/** Two action segments separated only by a blank-text (headless) lead. A
 *  turn the user interrupted mid-tool leaves these headless leads: the
 *  trailing-text run is empty, so no headline text precedes the tools. */
function headlessLedEvents(): WSEvent[] {
  const callA = "toolu_a";
  const callB = "toolu_b";
  return [
    agentMsg({
      type: "assistant",
      uuid: "lead-a",
      message: { content: [{ type: "text", text: "   " }] },
    }),
    agentMsg({
      type: "assistant",
      uuid: "tool-a",
      message: {
        content: [{ type: "tool_use", id: callA, name: "Bash", input: { command: "grep -rn MACHINES ." } }],
      },
    }),
    agentMsg({
      type: "user",
      uuid: "result-a",
      message: { content: [{ type: "tool_result", tool_use_id: callA, content: "a-out" }] },
    }),
    agentMsg({
      type: "assistant",
      uuid: "lead-b",
      message: { content: [{ type: "text", text: "" }] },
    }),
    agentMsg({
      type: "assistant",
      uuid: "tool-b",
      message: {
        content: [{ type: "tool_use", id: callB, name: "Bash", input: { command: "grep -n machines App.tsx" } }],
      },
    }),
    agentMsg({
      type: "user",
      uuid: "result-b",
      message: { content: [{ type: "tool_result", tool_use_id: callB, content: "b-out" }] },
    }),
  ];
}

describe("headless (blank-text) leads do not spawn separate action groups", () => {
  it("merges consecutive blank-lead tool segments into one group", () => {
    const msg = makeAssistantMsg({ id: "m-headless", content: "", events: headlessLedEvents() });
    const { container } = render(
      <MessageBubble message={msg} sessionId="s1" orchestrationMode="native" />,
    );
    const groups = container.querySelectorAll("[data-testid='auto-action-group']");
    expect(groups).toHaveLength(1);
    expect(groups[0].querySelector(".auto-action-group-count")?.textContent).toBe("2 actions");
  });

  it("empty (redacted) thinking blocks between tools do not split the group", () => {
    // Mirrors a real interleaved-thinking turn: a text lead, then tools
    // separated only by signature-only thinking blocks whose text is "".
    const calls = ["t1", "t2", "t3"];
    const events: WSEvent[] = [
      agentMsg({ type: "assistant", uuid: "lead", message: { content: [{ type: "text", text: "Checking the dev server." }] } }),
    ];
    calls.forEach((id, k) => {
      if (k > 0) {
        events.push(agentMsg({
          type: "assistant",
          uuid: `think-${k}`,
          message: { content: [{ type: "thinking", thinking: "", signature: "sig" }] },
        }));
      }
      events.push(agentMsg({
        type: "assistant",
        uuid: `tool-${k}`,
        message: { content: [{ type: "tool_use", id, name: "Bash", input: { command: `cmd${k}` } }] },
      }));
      events.push(agentMsg({
        type: "user",
        uuid: `res-${k}`,
        message: { content: [{ type: "tool_result", tool_use_id: id, content: `o${k}` }] },
      }));
    });
    const msg = makeAssistantMsg({ id: "m-think", content: "", events });
    const { container } = render(
      <MessageBubble message={msg} sessionId="s1" orchestrationMode="native" />,
    );
    const groups = container.querySelectorAll("[data-testid='auto-action-group']");
    expect(groups).toHaveLength(1);
    expect(groups[0].querySelector(".auto-action-group-count")?.textContent).toBe("3 actions");
  });

  it("a non-blank lead still starts its own separate group", () => {
    const callA = "toolu_x";
    const callB = "toolu_y";
    const msg = makeAssistantMsg({
      id: "m-mixed",
      content: "",
      events: [
        agentMsg({ type: "assistant", uuid: "l1", message: { content: [{ type: "text", text: "" }] } }),
        agentMsg({
          type: "assistant",
          uuid: "t1",
          message: { content: [{ type: "tool_use", id: callA, name: "Bash", input: { command: "ls" } }] },
        }),
        agentMsg({
          type: "user",
          uuid: "r1",
          message: { content: [{ type: "tool_result", tool_use_id: callA, content: "x" }] },
        }),
        agentMsg({
          type: "assistant",
          uuid: "l2",
          message: { content: [{ type: "text", text: "Now editing the file." }] },
        }),
        agentMsg({
          type: "assistant",
          uuid: "t2",
          message: { content: [{ type: "tool_use", id: callB, name: "Bash", input: { command: "cat x" } }] },
        }),
        agentMsg({
          type: "user",
          uuid: "r2",
          message: { content: [{ type: "tool_result", tool_use_id: callB, content: "y" }] },
        }),
      ],
    });
    const { container } = render(
      <MessageBubble message={msg} sessionId="s1" orchestrationMode="native" />,
    );
    expect(container.querySelectorAll("[data-testid='auto-action-group']")).toHaveLength(2);
  });
});
