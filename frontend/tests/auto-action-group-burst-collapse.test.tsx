import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import React from "react";
import { MessageBubble, TurnGroup } from "../src/components/MessageBubble";
import { makeAssistantMsg, makeUserMsg } from "./fixtures";
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

  it("renders failed paired-turn status under the response, not inside the prompt", () => {
    const initiator = makeUserMsg({
      id: "u-failed",
      content: "are the returned requirements valuable?",
      status: "error",
      errorText: "canceled",
    });
    const response = makeAssistantMsg({
      id: "a-failed",
      content: "",
      events: burstEvents(1),
    });
    const retry = vi.fn();
    const { container } = render(
      <TurnGroup
        initiatorMessage={initiator}
        responseMessage={response}
        sessionId="s1"
        onRetry={retry}
        orchestrationMode="native"
        isLatestTurnGroup
      />,
    );

    const prompt = container.querySelector('[data-testid="user-message"]');
    const responseBranch = container.querySelector(".turn-group-children");
    expect(prompt).not.toBeNull();
    expect(responseBranch).not.toBeNull();
    expect(prompt!.querySelector(".message-status.status-error")).toBeNull();
    expect(responseBranch!.querySelector(".message-status.status-error")).not.toBeNull();
    expect(responseBranch!.querySelector("[data-testid='auto-action-group']")).not.toBeNull();
  });
});
