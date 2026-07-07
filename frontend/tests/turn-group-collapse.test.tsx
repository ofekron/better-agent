import { describe, it, expect, afterEach } from "vitest";
import { act, fireEvent, render, cleanup, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "../src/i18n";
import { Chat } from "../src/components/Chat";
import { TurnGroup } from "../src/components/MessageBubble";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";
import { renderApp } from "./harness";

afterEach(cleanup);

describe("TurnGroup collapsed interrupted indicator", () => {
  it("collapses the latest completed chat group", async () => {
    const realFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async () =>
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ) as unknown as typeof fetch;
    const userMessage = makeUserMsg({ id: "u1", content: "latest prompt" });
    const assistantMessage = makeAssistantMsg({
      id: "a1",
      content: "finished reply",
      isStreaming: false,
    });

    try {
      const { container } = render(
        <Chat
          messages={[userMessage, assistantMessage]}
          pendingMessages={[]}
          runs={[]}
          streamingEvents={[]}
          traceSteps={[]}
          isStreaming={false}
          isStopping={false}
          streamingLoadPhase={null}
          onSend={() => true}
          disabled={false}
          session={makeSession()}
          draft=""
          onDraftChange={() => {}}
          queuedPrompt={null}
          onPromoteQueued={() => {}}
        />,
      );

      await waitFor(() => {
        expect(container.querySelector(".user-message-box > .message-box-body")).not.toBeNull();
        expect(container.querySelector(".assistant-message .message-content")).toBeNull();
        expect(container.textContent).toContain("finished reply");
        expect(container.querySelector(".collapse-arrow")?.textContent).toBe("▶");
      });
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  it("keeps historical collapsed groups compact when a later group exists", async () => {
    const realFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async () =>
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ) as unknown as typeof fetch;

    try {
      const { container } = render(
        <Chat
          messages={[
            makeUserMsg({ id: "u1", content: "older prompt" }),
            makeAssistantMsg({ id: "a1", content: "older reply", isStreaming: false }),
            makeUserMsg({ id: "u2", content: "latest prompt" }),
            makeAssistantMsg({ id: "a2", content: "streaming reply", isStreaming: true }),
          ]}
          pendingMessages={[]}
          runs={[{ run_id: "run-1", kind: "manager", target_message_id: "a2", pid: null }]}
          streamingEvents={[]}
          traceSteps={[]}
          isStreaming
          isStopping={false}
          streamingLoadPhase={null}
          onSend={() => true}
          disabled={false}
          session={makeSession()}
          draft=""
          onDraftChange={() => {}}
          queuedPrompt={null}
          onPromoteQueued={() => {}}
        />,
      );

      const firstGroup = container.querySelector<HTMLElement>('[data-message-id="u1"]')?.closest(".turn-group");
      expect(firstGroup).not.toBeNull();
      expect(firstGroup!.querySelector('.user-message-box > .message-box-body')).not.toBeNull();
      expect(firstGroup!.querySelector('.assistant-message .message-content')).toBeNull();
      expect(firstGroup!.querySelector('.collapse-arrow')?.textContent).toBe("▶");
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  it("keeps the user prompt body visible when the latest group auto-collapses", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "latest prompt" })}
        responseMessage={makeAssistantMsg({ id: "a1", content: "final reply" })}
        defaultCollapsed
        orchestrationMode="manager"
      />,
    );

    expect(container.querySelector(".user-message-box > .message-box-body")).not.toBeNull();
    expect(container.textContent).toContain("latest prompt");

    // The group chevron never folds the prompt text.
    fireEvent.click(screen.getByRole("button", { name: /User/i }));
    expect(container.querySelector(".user-message-box > .message-box-body")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /User/i }));
    expect(container.querySelector(".user-message-box > .message-box-body")).not.toBeNull();

    // Only the prompt's own chevron folds it, independently of the group.
    const promptToggle = container.querySelector<HTMLElement>(".prompt-collapse-toggle");
    expect(promptToggle).not.toBeNull();
    fireEvent.click(promptToggle!);
    expect(container.querySelector(".user-message-box > .message-box-body")).toBeNull();
    expect(container.querySelector(".message-box-collapsed-body")?.textContent).toContain("latest prompt");
    fireEvent.click(promptToggle!);
    expect(container.querySelector(".user-message-box > .message-box-body")).not.toBeNull();
  });

  it("collapses the latest assistant body when manually collapsed", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "latest prompt" })}
        responseMessage={makeAssistantMsg({ id: "a1", content: "final reply" })}
        defaultCollapsed={false}
        orchestrationMode="manager"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /User/i }));

    expect(container.querySelector(".user-message-box > .message-box-body")).not.toBeNull();
    expect(container.querySelector(".assistant-message .message-content")).toBeNull();
    expect(container.textContent).toContain("final reply");
    expect(container.querySelector(".collapse-arrow")?.textContent).toBe("▶");
  });

  it("auto-collapses a live latest group when the turn finishes", async () => {
    const userMessage = makeUserMsg({ id: "u1", content: "latest prompt" });
    const runningAssistant = makeAssistantMsg({
      id: "a1",
      content: "streaming reply",
      isStreaming: true,
    });
    const finishedAssistant = {
      ...runningAssistant,
      isStreaming: false,
      content: "finished reply",
    };

    const props = {
      pendingMessages: [],
      streamingEvents: [],
      traceSteps: [],
      isStopping: false,
      streamingLoadPhase: null,
      onSend: () => true,
      disabled: false,
      session: makeSession(),
      draft: "",
      onDraftChange: () => {},
      queuedPrompt: null,
      onPromoteQueued: () => {},
    } satisfies Partial<React.ComponentProps<typeof Chat>>;

    const { container, rerender } = render(
      <Chat
        {...props}
        messages={[userMessage, runningAssistant]}
        runs={[{ run_id: "run-1", kind: "manager", target_message_id: "a1", pid: null }]}
        isStreaming
      />,
    );

    expect(container.querySelector(".assistant-message .message-content")).not.toBeNull();
    expect(container.querySelector(".collapse-arrow")?.textContent).toBe("▼");

    rerender(
      <Chat
        {...props}
        messages={[userMessage, finishedAssistant]}
        runs={[]}
        isStreaming={false}
      />,
    );

    await waitFor(() => {
      expect(container.querySelector(".user-message-box > .message-box-body")).not.toBeNull();
      expect(container.querySelector(".assistant-message .message-content")).toBeNull();
      expect(container.textContent).toContain("finished reply");
      expect(container.querySelector(".collapse-arrow")?.textContent).toBe("▶");
    });
  });

  it("auto-collapses the active group after terminal websocket frames", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const userMessage = makeUserMsg({ id: "u1", content: "live prompt", seq: 0 });
    const assistantMessage = makeAssistantMsg({
      id: "a1",
      content: "streaming reply",
      seq: 1,
      isStreaming: true,
    });

    h.emitMany([
      { type: "turn_start", data: { app_session_id: session.id, manager_session_id: "agent-1" } },
      { type: "messages_replay", data: { app_session_id: session.id, messages: [userMessage, assistantMessage] } },
      {
        type: "run_state",
        data: {
          app_session_id: session.id,
          runs: [{ run_id: "run-1", kind: "manager", target_message_id: "a1", pid: null }],
        },
      },
    ]);
    await h.flush();

    expect(h.$('[data-testid="assistant-message"][data-message-id="a1"] .message-content')).not.toBeNull();

    h.emitMany([
      {
        type: "messages_delta",
        data: {
          app_session_id: session.id,
          messages: [{
            ...assistantMessage,
            content: "final reply",
            isStreaming: false,
            omitted_payloads: { events: { revision: "rev-1" } },
            events: undefined,
          }],
        },
      },
      { type: "turn_complete", data: { app_session_id: session.id, session_id: "agent-1", success: true } },
      { type: "run_state", data: { app_session_id: session.id, runs: [] } },
    ]);
    await h.flush();

    expect(h.$('[data-testid="user-message"][data-message-id="u1"] > .message-box-body')).not.toBeNull();
    expect(h.$('[data-testid="assistant-message"][data-message-id="a1"] .message-content')).toBeNull();
    expect(h.raw.container.textContent).toContain("final reply");
    expect(h.$(".collapse-arrow")?.textContent).toBe("▶");
    h.unmount();
  });

  it("collapses a completed latest group even while session monitoring remains active", async () => {
    const session = makeSession({
      messages: [
        makeUserMsg({ id: "u1", content: "done", seq: 0 }),
        makeAssistantMsg({ id: "a1", content: "finished reply", seq: 1, isStreaming: false }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "session_monitoring_changed",
      data: {
        session_id: session.id,
        monitoring_state: "active",
        cwd: session.cwd,
        node_id: session.node_id ?? "primary",
      },
    });
    await h.flush();

    expect(h.$('[data-testid="user-message"][data-message-id="u1"] > .message-box-body')).not.toBeNull();
    expect(h.$('[data-testid="assistant-message"][data-message-id="a1"] .message-content')).toBeNull();
    expect(h.raw.container.textContent).toContain("finished reply");
    expect(h.$(".collapse-arrow")?.textContent).toBe("▶");
    h.unmount();
  });

  it("shows the Interrupted box when the group is collapsed", () => {
    const userMessage = makeUserMsg({ id: "u1", content: "do a thing" });
    const assistantMessage = makeAssistantMsg({
      id: "a1",
      content: "partial reply",
      stopped_at: new Date("2024-01-01T10:00:00Z").toISOString(),
      interrupted_by_msg_id: "u2",
    });

    const { container } = render(
      <TurnGroup
        initiatorMessage={userMessage}
        responseMessage={assistantMessage}
        defaultCollapsed
        orchestrationMode="manager"
      />,
    );

    // Collapsed: the assistant message body is not mounted...
    expect(container.querySelector('[data-message-id="a1"] .message-content')).toBeNull();
    // ...but the Interrupted indicator must still be present.
    const indicator = container.querySelector(".stopped-indicator");
    expect(indicator).not.toBeNull();
    expect(indicator!.textContent).toContain("Interrupted");
  });

  it("shows assistant failures when the group is collapsed", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "do a thing" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "",
          error: true,
          errorText: "interrupted",
        })}
        defaultCollapsed
        orchestrationMode="native"
      />,
    );

    expect(container.querySelector('[data-message-id="a1"] .message-content')).toBeNull();
    const status = container.querySelector(".message-status.status-error");
    expect(status).not.toBeNull();
    expect(status!.textContent).toContain("Failed");
    expect(status!.textContent).toContain("interrupted");
  });

  it("prefers finalized assistant content over Bash event tails in collapsed preview", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "do a thing" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "Final TLDR visible to user",
          stub: {
            event_count: 3,
            last_events: [
              {
                type: "agent_message",
                data: {
                  type: "assistant",
                  message: {
                    role: "assistant",
                    content: [
                      {
                        type: "tool_use",
                        id: "tool-1",
                        name: "Bash",
                        input: { cmd: "git status" },
                      },
                    ],
                  },
                },
              },
              {
                type: "agent_message",
                data: {
                  type: "user",
                  message: {
                    role: "user",
                    content: [
                      {
                        type: "tool_result",
                        tool_use_id: "tool-1",
                        content: "Bash output should not be the preview",
                      },
                    ],
                  },
                },
              },
              {
                type: "agent_message",
                data: {
                  type: "assistant",
                  message: {
                    role: "assistant",
                    content: [
                      {
                        type: "text",
                        text: "Final TLDR visible to user",
                      },
                    ],
                  },
                },
              },
            ],
          },
        })}
        defaultCollapsed
        orchestrationMode="native"
      />,
    );

    expect(container.textContent).toContain("Final TLDR visible to user");
    expect(container.textContent).not.toContain("Bash output should not be the preview");
  });

  it("renders escaped unicode bullet separators as bullets", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "review" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "before\n\\u2022 \\u2022 \\u2022\nafter",
        })}
        orchestrationMode="manager"
      />,
    );

    expect(container.textContent).toContain("• • •");
    expect(container.textContent).not.toContain("\\u2022");
  });

  it("renders escaped unicode bullets inside sub-session panels as bullets", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "review" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          workers: [
            {
              delegation_id: "sub-1",
              worker_session_id: "session-a",
              worker_description: "Adversarial review",
              panel_kind: "sub_session",
              is_new: false,
              instructions_preview: "",
              events: [
                {
                  type: "output",
                  data: { output: "Residual risks:\n\\u2022 first\n\\u2022 second" },
                },
              ],
            },
          ],
        })}
        orchestrationMode="manager"
      />,
    );

    expect(container.querySelector(".collapse-ellipsis")?.textContent).toBe("• • •");
    expect(container.textContent).not.toContain("\\u2022");

    fireEvent.click(screen.getByRole("button", { name: /Adversarial review/i }));

    expect(container.textContent).toContain("• first");
    expect(container.textContent).toContain("• second");
    expect(container.textContent).not.toContain("\\u2022");
  });

  it("auto-collapses a nested sub-session panel after its worker run completes", async () => {
    const worker = {
      delegation_id: "sub-1",
      worker_session_id: "session-a",
      worker_description: "Adversarial review",
      panel_kind: "sub_session" as const,
      is_new: false,
      instructions_preview: "",
      events: [
        {
          type: "output" as const,
          data: { output: "running review output" },
        },
      ],
    };
    const { container, rerender } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "review" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          workers: [worker],
        })}
        runs={[
          {
            run_id: "run-sub-1",
            kind: "worker",
            target_message_id: "a1",
            delegation_id: "sub-1",
            pid: null,
            started_at: "2026-07-03T00:00:00Z",
            last_event_at: "2026-07-03T00:00:01Z",
          },
        ]}
        orchestrationMode="manager"
      />,
    );

    expect(screen.getByRole("button", { name: /Adversarial review/i }).getAttribute("aria-expanded")).toBe("true");
    expect(container.textContent).toContain("running review output");

    rerender(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "review" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          workers: [{ ...worker, success: true }],
        })}
        runs={[]}
        orchestrationMode="manager"
      />,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Adversarial review/i }).getAttribute("aria-expanded")).toBe("false");
    });
    expect(container.querySelector(".collapse-ellipsis")?.textContent).toBe("• • •");
  });

  it("auto-collapses a nested native sub-agent block after its tool result arrives", async () => {
    const events = [
      {
        type: "agent_message" as const,
        data: {
          type: "assistant",
          message: {
            content: [
              {
                type: "tool_use",
                id: "tool-1",
                name: "Task",
                input: { description: "review" },
              },
            ],
          },
        },
      },
      {
        type: "agent_message" as const,
        data: {
          type: "assistant",
          parent_tool_use_id: "tool-1",
          message: {
            content: [{ type: "text", text: "nested review output" }],
          },
        },
      },
    ];
    const { container, rerender } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "review" })}
        responseMessage={makeAssistantMsg({ id: "a1", events })}
        orchestrationMode="manager"
      />,
    );

    expect(container.querySelector(".sub-agent-header")?.getAttribute("aria-expanded")).toBe("true");
    expect(container.textContent).toContain("nested review output");

    rerender(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "review" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          events: [
            ...events,
            {
              type: "agent_message" as const,
              data: {
                type: "user",
                message: {
                  content: [
                    {
                      type: "tool_result",
                      tool_use_id: "tool-1",
                      content: "done",
                    },
                  ],
                },
              },
            },
          ],
        })}
        orchestrationMode="manager"
      />,
    );

    await waitFor(() => {
      expect(container.querySelector(".sub-agent-header")?.getAttribute("aria-expanded")).toBe("false");
    });
    expect(container.querySelector(".sub-agent-block .collapse-ellipsis")?.textContent).toBe("• • •");
  });

  it("renders creation-only sub-session panels without an empty expand toggle", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "review" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          workers: [
            {
              delegation_id: "sub-created-1",
              worker_session_id: "session-a",
              worker_description: "Adversarial review for ask button UI/mobile fix created",
              panel_kind: "sub_session_created",
              is_new: true,
              instructions_preview: "",
              events: [],
            },
          ],
        })}
        orchestrationMode="manager"
      />,
    );

    expect(container.textContent).toContain("Sub Session Created");
    expect(container.textContent).toContain("Adversarial review for ask button UI/mobile fix created");
    expect(screen.queryByRole("button", { name: /Adversarial review/i })).toBeNull();
    expect(container.querySelector(".timeline-static-header")).not.toBeNull();
    expect(container.querySelector(".timeline-toggle-header")).toBeNull();
    expect(container.querySelector(".collapsible-timeline-block .collapse-arrow")).toBeNull();
  });

  it("renders collapsed sub-agent ellipsis as bullets", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "review" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          events: [
            {
              type: "tool_call",
              data: {
                tool: "Task",
                args: { description: "review" },
                tool_use_id: "tool-1",
              },
            },
            {
              type: "output",
              data: {
                output: "sub-agent output",
                parent_tool_use_id: "tool-1",
              },
            },
          ],
        })}
        orchestrationMode="manager"
      />,
    );

    const header = container.querySelector<HTMLElement>(".sub-agent-header");
    expect(header).not.toBeNull();
    fireEvent.click(header!);

    expect(container.querySelector(".sub-agent-block .collapse-ellipsis")?.textContent).toBe("• • •");
    expect(container.textContent).not.toContain("\\u2022");
  });

  it("edits and submits an alter from the user message bubble", async () => {
    const onAlterUserMessage = vi.fn(() => true);
    render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "old prompt" })}
        responseMessage={makeAssistantMsg({ id: "a1", content: "done" })}
        onAlterTurnMessage={onAlterUserMessage}
        orchestrationMode="manager"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Alter" }));
    const editor = screen.getByDisplayValue("old prompt");
    fireEvent.change(editor, { target: { value: "new prompt" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Send altered" }));
    });

    expect(onAlterUserMessage).toHaveBeenCalledTimes(1);
    expect(onAlterUserMessage.mock.calls[0][0].id).toBe("u1");
    expect(onAlterUserMessage.mock.calls[0][1]).toBe("new prompt");
  });

  it("shows Alter only on the latest user prompt in Chat", () => {
    render(
      <Chat
        messages={[
          makeUserMsg({ id: "u1", content: "first prompt" }),
          makeAssistantMsg({ id: "a1", content: "first reply" }),
          makeUserMsg({ id: "u2", content: "latest prompt" }),
          makeAssistantMsg({ id: "a2", content: "latest reply" }),
        ]}
        pendingMessages={[]}
        runs={[]}
        streamingEvents={[]}
        traceSteps={[]}
        isStreaming={false}
        isStopping={false}
        streamingLoadPhase={null}
        onSend={() => true}
        onAlterUserMessage={() => true}
        disabled={false}
        session={makeSession()}
        draft=""
        onDraftChange={() => {}}
        queuedPrompt={null}
        onPromoteQueued={() => {}}
      />,
    );

    const userMessages = screen.getAllByTestId("user-message");
    expect(within(userMessages[0]).queryByRole("button", { name: "Alter" })).toBeNull();
    expect(within(userMessages[1]).getByRole("button", { name: "Alter" })).toBeTruthy();
  });

  it("shows 'Stopped' (not Interrupted) when stopped without an interrupting message", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "partial",
          stopped_at: new Date("2024-01-01T10:00:00Z").toISOString(),
        })}
        defaultCollapsed
        orchestrationMode="manager"
      />,
    );

    const indicator = container.querySelector(".stopped-indicator");
    expect(indicator).not.toBeNull();
    expect(indicator!.textContent).toContain("Stopped");
    expect(indicator!.textContent).not.toContain("Interrupted");
  });

  it("renders no stopped indicator for a collapsed group that completed normally", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1" })}
        responseMessage={makeAssistantMsg({ id: "a1", content: "done" })}
        defaultCollapsed
        orchestrationMode="manager"
      />,
    );

    expect(container.querySelector(".stopped-indicator")).toBeNull();
  });

  it("shows steer prompts when the group is collapsed", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "start work" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          content: "final answer after steer",
          events: [
            {
              type: "steer_prompt",
              data: {
                uuid: "steer-1",
                prompt: "also include queued interrupt",
              },
            },
            {
              type: "agent_message",
              data: {
                uuid: "a-event-1",
                type: "assistant",
                message: {
                  content: [
                    {
                      type: "text",
                      text: "final answer after steer",
                    },
                  ],
                },
              },
            },
          ],
        })}
        defaultCollapsed
        orchestrationMode="native"
      />,
    );

    expect(container.querySelector('[data-message-id="a1"] .message-content')).toBeNull();
    expect(container.querySelector(".event-steer-prompt")?.textContent).toContain(
      "also include queued interrupt",
    );
    expect(container.textContent).toContain("final answer after steer");
  });

  it("uses nested child output as the collapsed final preview", () => {
    const { container } = render(
      <TurnGroup
        initiatorMessage={makeUserMsg({ id: "u1", content: "delegate work" })}
        responseMessage={makeAssistantMsg({
          id: "a1",
          events: [
            {
              type: "output",
              data: {
                output: "outer setup",
              },
            },
            {
              type: "tool_call",
              data: {
                tool: "delegate",
                args: {},
                tool_use_id: "tool-1",
              },
            },
            {
              type: "output",
              data: {
                output: "nested final text chunk",
                parent_tool_use_id: "tool-1",
              },
            },
          ],
        })}
        defaultCollapsed
        orchestrationMode="manager"
      />,
    );

    expect(container.querySelector('[data-message-id="a1"] .message-content')).toBeNull();
    expect(container.textContent).toContain("nested final text chunk");
    expect(container.textContent).not.toContain("outer setup");
  });

  it("uses stub tail events while collapsed and fetches full events only after expand", async () => {
    const realFetch = globalThis.fetch;
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify(makeAssistantMsg({
          id: "a1",
          content: "full content",
          events: [
            {
              type: "output",
              data: { output: "full expanded output" },
            },
          ],
        })),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    try {
      const { container } = render(
        <TurnGroup
          initiatorMessage={makeUserMsg({ id: "u1", content: "expensive history" })}
          responseMessage={makeAssistantMsg({
            id: "a1",
            content: "stale fallback content",
            events: [],
            stub: {
              event_count: 40,
              last_events: [
                {
                  type: "output",
                  data: { output: "stub collapsed preview" },
                },
              ],
            },
          })}
          defaultCollapsed
          sessionId="s1"
          orchestrationMode="native"
        />,
      );

      expect(fetchMock).not.toHaveBeenCalled();
      expect(container.textContent).toContain("stub collapsed preview");
      expect(container.textContent).not.toContain("stale fallback content");

      fireEvent.click(screen.getByRole("button", { name: /User/i }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledTimes(1);
      });
      expect(fetchMock.mock.calls[0][0]).toBe(
        "/api/sessions/s1/messages/a1/events",
      );
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  it("fetches projected non-stub events only after collapsed group expands", async () => {
    const realFetch = globalThis.fetch;
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify(makeAssistantMsg({
          id: "a1",
          content: "full content",
          events: [
            {
              type: "output",
              data: { output: "full projected output" },
            },
          ],
        })),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    try {
      const { container } = render(
        <TurnGroup
          initiatorMessage={makeUserMsg({ id: "u1", content: "expensive history" })}
          responseMessage={makeAssistantMsg({
            id: "a1",
            content: "stale fallback content",
            events: undefined,
            omitted_payloads: { events: { revision: "rev-1" } },
          })}
          defaultCollapsed
          sessionId="s1"
          orchestrationMode="native"
        />,
      );

      expect(fetchMock).not.toHaveBeenCalled();
      expect(container.textContent).toContain("stale fallback content");
      expect(container.textContent).not.toContain("full projected output");

      fireEvent.click(screen.getByRole("button", { name: /User/i }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledTimes(1);
        expect(container.textContent).toContain("full projected output");
      });
      expect(fetchMock.mock.calls[0][0]).toBe(
        "/api/sessions/s1/messages/a1/events",
      );
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  it("keeps projected events hydrated across manual collapse and expand", async () => {
    const realFetch = globalThis.fetch;
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify(makeAssistantMsg({
          id: "a1",
          content: "full content",
          events: [
            {
              type: "output",
              data: { output: "manual full output" },
            },
          ],
        })),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    try {
      const { container } = render(
        <TurnGroup
          initiatorMessage={makeUserMsg({ id: "u1", content: "manual prompt" })}
          responseMessage={makeAssistantMsg({
            id: "a1",
            content: "stale fallback content",
            events: undefined,
            omitted_payloads: { events: { revision: "rev-1" } },
          })}
          defaultCollapsed={false}
          sessionId="s1"
          orchestrationMode="native"
        />,
      );

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledTimes(1);
        expect(container.textContent).toContain("manual full output");
      });

      fireEvent.click(screen.getByRole("button", { name: /User/i }));
      expect(container.querySelector(".assistant-message .message-content")).toBeNull();

      fireEvent.click(screen.getByRole("button", { name: /User/i }));
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(container.textContent).toContain("manual full output");
    } finally {
      globalThis.fetch = realFetch;
    }
  });
});
