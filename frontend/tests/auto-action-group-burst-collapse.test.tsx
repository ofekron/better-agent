import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import React from "react";
import { MessageBubble } from "../src/components/MessageBubble";
import { makeAssistantMsg } from "./fixtures";
import type { WSEvent } from "../src/types";

function agentMsg(data: Record<string, unknown>): WSEvent {
  return { type: "agent_message", data } as WSEvent;
}

/** A text lead followed by `n` Bash tool calls (each with its result), and no
 *  later text lead — so the run is the final/latest action group, the exact
 *  shape of an in-flight burst. */
function burstEvents(n: number): WSEvent[] {
  const events: WSEvent[] = [
    agentMsg({
      type: "assistant",
      uuid: "lead",
      message: { content: [{ type: "text", text: "Digging into this." }] },
    }),
  ];
  for (let k = 0; k < n; k++) {
    const id = `toolu_${k}`;
    events.push(
      agentMsg({
        type: "assistant",
        uuid: `tool-${k}`,
        message: { content: [{ type: "tool_use", id, name: "Bash", input: { command: `cmd${k}` } }] },
      }),
    );
    events.push(
      agentMsg({
        type: "user",
        uuid: `res-${k}`,
        message: { content: [{ type: "tool_result", tool_use_id: id, content: `o${k}` }] },
      }),
    );
  }
  return events;
}

function renderGroup(n: number) {
  const msg = makeAssistantMsg({ id: `m-burst-${n}`, content: "", events: burstEvents(n) });
  const { container } = render(
    <MessageBubble message={msg} sessionId="s1" orchestrationMode="native" />,
  );
  return container.querySelector("[data-testid='auto-action-group']") as HTMLElement | null;
}

describe("action group burst collapse", () => {
  it("a small group (<=3 actions) stays open by default", () => {
    const group = renderGroup(2);
    expect(group).not.toBeNull();
    expect(group!.querySelector(".auto-action-group-count")?.textContent).toBe("2 actions");
    expect(group!.classList.contains("open")).toBe(true);
    expect(group!.querySelector(".auto-action-group-header")?.getAttribute("aria-expanded")).toBe("true");
  });

  it("a burst (>3 actions) auto-collapses into a single header", () => {
    const group = renderGroup(5);
    expect(group).not.toBeNull();
    expect(group!.querySelector(".auto-action-group-count")?.textContent).toBe("5 actions");
    expect(group!.classList.contains("open")).toBe(false);
    expect(group!.querySelector(".auto-action-group-header")?.getAttribute("aria-expanded")).toBe("false");
    // Collapsed by default: the individual tool cards are hidden.
    expect(group!.querySelectorAll(".tool-call")).toHaveLength(0);
  });
});
