import { describe, it, expect, afterEach } from "vitest";
import { render, fireEvent, cleanup } from "@testing-library/react";
import React from "react";
import { MessageBubble } from "../src/components/MessageBubble";
import { makeAssistantMsg } from "./fixtures";
import type { WSEvent } from "../src/types";

afterEach(cleanup);

function agentMsg(data: Record<string, unknown>): WSEvent {
  return { type: "agent_message", data } as WSEvent;
}

function leadWithTools(): WSEvent[] {
  const events: WSEvent[] = [
    agentMsg({
      type: "assistant",
      uuid: "lead",
      message: { content: [{ type: "text", text: "Digging into this." }] },
    }),
  ];
  for (let k = 0; k < 2; k++) {
    const id = `toolu_${k}`;
    events.push(
      agentMsg({
        type: "assistant",
        uuid: `tool-${k}`,
        message: {
          content: [{ type: "tool_use", id, name: "Bash", input: { command: `cmd${k}` } }],
        },
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

function laterLead(): WSEvent {
  return agentMsg({
    type: "assistant",
    uuid: "lead-2",
    message: { content: [{ type: "text", text: "Moving on to the next step." }] },
  });
}

function renderMessage(events: WSEvent[]) {
  const msg = makeAssistantMsg({ id: "m-toggle", content: "", events });
  return render(<MessageBubble message={msg} sessionId="s1" orchestrationMode="native" />);
}

function firstGroup(container: HTMLElement): HTMLElement {
  const group = container.querySelector("[data-testid='auto-action-group']");
  expect(group).not.toBeNull();
  return group as HTMLElement;
}

describe("AutoActionGroup user toggle vs compact default", () => {
  it("completed groups render compact by default (no size-based auto-open)", () => {
    const { container } = renderMessage(leadWithTools());
    const header = firstGroup(container).querySelector(
      ".auto-action-group-header",
    ) as HTMLElement;
    expect(header.getAttribute("aria-expanded")).toBe("false");
  });

  it("keeps a user-opened group open when a later lead arrives", () => {
    const { container, rerender } = renderMessage(leadWithTools());
    const group = firstGroup(container);
    const header = group.querySelector(".auto-action-group-header") as HTMLElement;

    // User expands — an explicit "keep this open" choice.
    fireEvent.click(header);
    expect(header.getAttribute("aria-expanded")).toBe("true");

    const msg = makeAssistantMsg({
      id: "m-toggle",
      content: "",
      events: [...leadWithTools(), laterLead()],
    });
    rerender(<MessageBubble message={msg} sessionId="s1" orchestrationMode="native" />);

    const headerAfter = firstGroup(container).querySelector(
      ".auto-action-group-header",
    ) as HTMLElement;
    expect(headerAfter.getAttribute("aria-expanded")).toBe("true");
  });

  it("keeps an untouched group compact when a later lead arrives", () => {
    const { container, rerender } = renderMessage(leadWithTools());
    const header = firstGroup(container).querySelector(
      ".auto-action-group-header",
    ) as HTMLElement;
    expect(header.getAttribute("aria-expanded")).toBe("false");

    const msg = makeAssistantMsg({
      id: "m-toggle",
      content: "",
      events: [...leadWithTools(), laterLead()],
    });
    rerender(<MessageBubble message={msg} sessionId="s1" orchestrationMode="native" />);

    const headerAfter = firstGroup(container).querySelector(
      ".auto-action-group-header",
    ) as HTMLElement;
    expect(headerAfter.getAttribute("aria-expanded")).toBe("false");
  });
});
