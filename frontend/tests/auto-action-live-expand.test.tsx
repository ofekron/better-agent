import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import React from "react";
import { TurnGroup } from "../src/components/MessageBubble";
import { makeAssistantMsg, makeUserMsg } from "./fixtures";
import type { WSEvent } from "../src/types";

afterEach(cleanup);

/**
 * chat-panel.md render model: while a turn is live, every item on the path
 * from the turn root to its final live leaf is forced fully expanded — any
 * size. When live ends, ordinary compact rules apply: completed groups
 * render collapsed and expand only on explicit user intent (no size-based
 * auto-open heuristics).
 */

function agentMsg(data: Record<string, unknown>): WSEvent {
  return { type: "agent_message", data } as WSEvent;
}

function leadWithManyTools(count: number): WSEvent[] {
  const events: WSEvent[] = [
    agentMsg({
      type: "assistant",
      uuid: "lead",
      message: { content: [{ type: "text", text: "Working through a long batch." }] },
    }),
  ];
  for (let k = 0; k < count; k++) {
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

function renderTurn({ running }: { running: boolean }) {
  const initiator = makeUserMsg({ id: "u-live", content: "go" });
  const response = makeAssistantMsg({
    id: "a-live",
    content: "",
    events: leadWithManyTools(5),
    isStreaming: running,
  });
  return render(
    <TurnGroup
      initiatorMessage={initiator}
      responseMessage={response}
      sessionId="s1"
      orchestrationMode="native"
      sessionRunning={running}
      activelyStreaming={running}
      defaultCollapsed={false}
    />,
  );
}

function groupHeader(container: HTMLElement): HTMLElement {
  const group = container.querySelector("[data-testid='auto-action-group']");
  expect(group).not.toBeNull();
  return (group as HTMLElement).querySelector(".auto-action-group-header") as HTMLElement;
}

describe("live-leaf force expansion vs completed compact default", () => {
  it("keeps the live leaf group expanded regardless of action count", () => {
    const { container } = renderTurn({ running: true });
    expect(groupHeader(container).getAttribute("aria-expanded")).toBe("true");
  });

  it("renders the group compact once the turn is no longer live", () => {
    const { container } = renderTurn({ running: false });
    expect(groupHeader(container).getAttribute("aria-expanded")).toBe("false");
  });
});
