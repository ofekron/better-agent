import { describe, it, expect, vi } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import React from "react";
import { MessageBubble } from "../src/components/MessageBubble";
import { makeAssistantMsg } from "./fixtures";
import type { WSEvent } from "../src/types";

const TASK_ID = "toolu_task_1";
const READ_ID = "toolu_read_1";
const NARRATION = "Now let me check the session bridge spec for the stream closed meaning";
const READ_RESULT = "REAL READ RESULT CONTENT";
const FULL_HISTORY_FORK_ERROR =
  "Full-history forked agents inherit the parent agent type, model, and reasoning effort; omit agent_type, model, and reasoning_effort, or spawn without a full-history fork.";

function agentMsg(data: Record<string, unknown>): WSEvent {
  return { type: "agent_message", data } as WSEvent;
}

/** Native claude-shaped stream: a top-level Agent call whose sidechain
 *  subagent runs Read (with a real tool_result) and then narrates. */
function makeEvents(): WSEvent[] {
  return [
    agentMsg({
      type: "assistant",
      uuid: "u-1",
      message: {
        content: [
          {
            type: "tool_use",
            id: TASK_ID,
            name: "Agent",
            input: { subagent_type: "Explore", description: "find spec" },
          },
        ],
      },
    }),
    agentMsg({
      type: "assistant",
      uuid: "u-2",
      parent_tool_use_id: TASK_ID,
      message: {
        content: [
          { type: "tool_use", id: READ_ID, name: "Read", input: { file_path: "/tmp/spec.md" } },
        ],
      },
    }),
    agentMsg({
      type: "user",
      uuid: "u-3",
      parent_tool_use_id: TASK_ID,
      message: {
        content: [{ type: "tool_result", tool_use_id: READ_ID, content: READ_RESULT }],
      },
    }),
    agentMsg({
      type: "assistant",
      uuid: "u-4",
      parent_tool_use_id: TASK_ID,
      message: { content: [{ type: "text", text: NARRATION }] },
    }),
    agentMsg({
      type: "user",
      uuid: "u-5",
      message: {
        content: [{ type: "tool_result", tool_use_id: TASK_ID, content: "subagent finished" }],
      },
    }),
  ];
}

function renderMsg() {
  const msg = makeAssistantMsg({ id: "m1", content: "done", events: makeEvents() });
  return render(<MessageBubble message={msg} sessionId="s1" orchestrationMode="native" />);
}

function resultContainersWith(container: HTMLElement, text: string): HTMLElement[] {
  return (Array.from(
    container.querySelectorAll(".tool-result-inline, .tool-result-text, .tool-result"),
  ) as HTMLElement[]).filter((el) => el.textContent?.includes(text));
}

describe("subagent text events render standalone, not as tool results", () => {
  it("assistant errorText without content renders as visible output", () => {
    const msg = makeAssistantMsg({
      id: "err",
      content: "",
      error: true,
      errorText: "gemini CLI not found on PATH",
    });
    const { container } = render(
      <MessageBubble message={msg} sessionId="s1" orchestrationMode="native" />,
    );
    expect(container.querySelector(".assistant-message .message-box")?.textContent).toContain(
      "gemini CLI not found on PATH",
    );
    expect(container.querySelector(".message-status.status-error")).toBeTruthy();
  });

  it("renders finalized assistant content after a tool-only timeline", () => {
    const summary = "**Executive summary**\n\nThe final answer is visible.";
    const msg = makeAssistantMsg({
      id: "summary-after-tool",
      content: summary,
      events: [
        agentMsg({
          type: "assistant",
          uuid: "tool-only",
          message: {
            role: "assistant",
            content: [{
              type: "tool_use",
              id: "tool-only-call",
              name: "Bash",
              input: { cmd: "git status --short" },
            }],
          },
        }),
      ],
    });

    const { container } = render(
      <MessageBubble message={msg} sessionId="s1" orchestrationMode="native" />,
    );

    expect(container.textContent).toContain("Executive summary");
    expect(container.textContent).toContain("The final answer is visible.");
  });

  it("does not duplicate finalized assistant content already rendered from split output events", () => {
    const msg = makeAssistantMsg({
      id: "split-output",
      content: "Part one\nPart two",
      events: [
        agentMsg({
          type: "assistant",
          uuid: "part-one",
          message: {
            role: "assistant",
            content: [{ type: "text", text: "Part one" }],
          },
        }),
        agentMsg({
          type: "assistant",
          uuid: "part-two",
          message: {
            role: "assistant",
            content: [{ type: "text", text: "Part two" }],
          },
        }),
      ],
    });

    const { container } = render(
      <MessageBubble message={msg} sessionId="s1" orchestrationMode="native" />,
    );

    expect(container.textContent?.match(/Part one/g)).toHaveLength(1);
    expect(container.textContent?.match(/Part two/g)).toHaveLength(1);
  });

  it("nested subagent narration is NOT placed inside a tool result", () => {
    const { container } = renderMsg();
    expect(container.textContent).toContain(NARRATION);
    expect(resultContainersWith(container, NARRATION)).toHaveLength(0);
  });

  it("nested subagent tool_call pairs with its real tool_result by id", async () => {
    const { container } = renderMsg();
    fireEvent.click(container.querySelector(".sub-agent-header") as HTMLElement);
    await waitFor(() => {
      expect(resultContainersWith(container, READ_RESULT).length).toBeGreaterThan(0);
    });
  });

  it("renders without duplicate React keys (paired tool groups don't collide with the next group)", () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      renderMsg();
      const dupKey = errSpy.mock.calls.find((args) =>
        args.some((a) => typeof a === "string" && a.includes("same key")),
      );
      expect(dupKey).toBeUndefined();
    } finally {
      errSpy.mockRestore();
    }
  });

  it("collapsed sub-agent preview does not show narration as a tool result", () => {
    const { container } = renderMsg();
    const header = container.querySelector(".sub-agent-header") as HTMLElement;
    expect(header).toBeTruthy();
    fireEvent.click(header);
    expect(resultContainersWith(container, NARRATION)).toHaveLength(0);
  });

  it("renders failed Agent spawn retries as a distinct failed state", async () => {
    const failedCallId = "call_failed_spawn";
    const successCallId = "call_success_spawn";
    const prompt = "Adversarially review recent work";
    const msg = makeAssistantMsg({
      id: "retry",
      content: "",
      events: [
        agentMsg({
          type: "assistant",
          uuid: "u-failed-call",
          message: {
            content: [{
              type: "tool_use",
              id: failedCallId,
              name: "Agent",
              input: {
                agent_type: "default",
                fork_context: true,
                subagent_type: "default",
                description: prompt,
                prompt,
              },
            }],
          },
        }),
        agentMsg({
          type: "user",
          uuid: "u-failed-result",
          message: {
            content: [{ type: "tool_result", tool_use_id: failedCallId, content: FULL_HISTORY_FORK_ERROR }],
          },
        }),
        agentMsg({
          type: "assistant",
          uuid: "u-success-call",
          message: {
            content: [{
              type: "tool_use",
              id: successCallId,
              name: "Agent",
              input: {
                fork_context: true,
                subagent_type: "default",
                description: prompt,
                prompt,
              },
            }],
          },
        }),
        agentMsg({
          type: "user",
          uuid: "u-success-result",
          message: {
            content: [{ type: "tool_result", tool_use_id: successCallId, content: JSON.stringify({ agent_id: "agent-1", nickname: "Mendel" }) }],
          },
        }),
      ],
    });

    const { container } = render(
      <MessageBubble message={msg} sessionId="s1" orchestrationMode="native" />,
    );
    await waitFor(() => {
      expect(container.querySelectorAll(".agent-tool-call")).toHaveLength(2);
    });
    const cards = Array.from(container.querySelectorAll(".agent-tool-call")) as HTMLElement[];

    expect(cards).toHaveLength(2);
    expect(cards[0].classList.contains("agent-tool-call-failed")).toBe(true);
    expect(cards[0].textContent).toContain("toolCall.agentSpawnFailed");
    expect(cards[0].textContent).toContain("Full-history forked agents inherit");
    expect(cards[1].classList.contains("agent-tool-call-failed")).toBe(false);
    expect(cards[1].textContent).toContain("default");
    expect(cards[1].textContent).toContain("Mendel");
  });

  it("auto-collapses a completed text-led action group when later text arrives", async () => {
    const readCallId = "read-call";
    const msg = makeAssistantMsg({
      id: "grouping",
      content: "",
      events: [
        agentMsg({
          type: "assistant",
          uuid: "lead",
          message: { content: [{ type: "text", text: "I will inspect the file first." }] },
        }),
        agentMsg({
          type: "assistant",
          uuid: "tool",
          message: {
            content: [{
              type: "tool_use",
              id: readCallId,
              name: "Read",
              input: { file_path: "/tmp/spec.md" },
            }],
          },
        }),
        agentMsg({
          type: "user",
          uuid: "result",
          message: {
            content: [{ type: "tool_result", tool_use_id: readCallId, content: READ_RESULT }],
          },
        }),
        agentMsg({
          type: "assistant",
          uuid: "next-text",
          message: { content: [{ type: "text", text: "Now I have enough context." }] },
        }),
      ],
    });

    const { container } = render(
      <MessageBubble message={msg} sessionId="s1" orchestrationMode="native" />,
    );
    const group = container.querySelector("[data-testid='auto-action-group']") as HTMLElement;
    expect(group).toBeTruthy();
    const toggle = group.querySelector(".auto-action-group-header") as HTMLElement;
    const body = group.querySelector(".auto-action-group-body") as HTMLElement;
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(toggle.textContent).toContain("I will inspect the file first.");
    expect(toggle.querySelectorAll(".collapse-arrow")).toHaveLength(1);
    expect(body).toBeNull();
    expect(container.textContent).toContain("Now I have enough context.");
    expect(resultContainersWith(container, READ_RESULT)).toHaveLength(0);

    fireEvent.click(toggle);
    await waitFor(() => {
      expect(resultContainersWith(container, READ_RESULT).length).toBeGreaterThan(0);
    });
    const expandedBody = group.querySelector(".auto-action-group-body") as HTMLElement;
    expect(expandedBody.textContent).not.toContain("I will inspect the file first.");
  });

  it("adds jump-up controls from action rows to the lead text", async () => {
    const readCallId = "read-call";
    const msg = makeAssistantMsg({
      id: "grouping-jump",
      content: "",
      events: [
        agentMsg({
          type: "assistant",
          uuid: "lead",
          message: { content: [{ type: "text", text: "Inspect before editing." }] },
        }),
        agentMsg({
          type: "assistant",
          uuid: "tool",
          message: {
            content: [{
              type: "tool_use",
              id: readCallId,
              name: "Read",
              input: { file_path: "/tmp/spec.md" },
            }],
          },
        }),
        agentMsg({
          type: "user",
          uuid: "result",
          message: {
            content: [{ type: "tool_result", tool_use_id: readCallId, content: READ_RESULT }],
          },
        }),
        agentMsg({
          type: "assistant",
          uuid: "next-text",
          message: { content: [{ type: "text", text: "Now I have enough context." }] },
        }),
      ],
    });

    const { container } = render(
      <MessageBubble message={msg} sessionId="s1" orchestrationMode="native" />,
    );
    const group = container.querySelector("[data-testid='auto-action-group']") as HTMLElement;
    fireEvent.click(group.querySelector(".auto-action-group-header") as HTMLElement);

    await waitFor(() => {
      expect(group.querySelector(".auto-action-group-body")).toBeTruthy();
    });

    const lead = group.querySelector(".auto-action-group-lead") as HTMLElement;
    const jump = group.querySelector(".auto-action-group-body .jump-to-parent-btn") as HTMLElement;
    expect(lead.id).toMatch(/^action-lead-/);
    expect(jump).toBeTruthy();
  });
});
