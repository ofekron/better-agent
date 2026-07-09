import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import React from "react";
import { MessageBubble } from "../src/components/MessageBubble";
import { makeAssistantMsg } from "./fixtures";
import type { WSEvent } from "../src/types";

describe("unknown lifecycle events are not rendered", () => {
  const cases: Array<{ type: WSEvent["type"]; data: Record<string, unknown> }> = [
    { type: "turn_started", data: { turn_id: "t1", message_id: "a", source_ts: "2026-01-01T00:00:00Z" } },
    { type: "turn_stopped", data: { app_session_id: "s1", stopped_at: "2026-01-01T00:00:00Z", workers_used: [] } },
    { type: "turn_detached", data: { app_session_id: "s1", msg_id: "a" } },
  ];

  it.each(cases)("renders $type as nothing (no 'unknown event' card)", ({ type, data }) => {
    const message = makeAssistantMsg({
      id: "a",
      content: "",
      events: [{ type, data }],
    });
    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        orchestrationMode: "native",
      }),
    );

    expect(container.querySelector(".event-diagnostic")).toBeNull();
    expect(container.textContent ?? "").not.toContain("unknown event");

    unmount();
  });

  it("renders all three together without any 'unknown event' card", () => {
    const message = makeAssistantMsg({
      id: "a",
      content: "",
      events: cases.map(({ type, data }) => ({ type, data })),
    });
    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        orchestrationMode: "native",
      }),
    );

    expect(container.querySelector(".event-diagnostic")).toBeNull();
    expect(container.textContent ?? "").not.toContain("unknown event");

    unmount();
  });

  it("routes leaked worker_event wrappers into their worker panel", () => {
    const message = makeAssistantMsg({
      id: "a",
      content: "",
      events: [{
        type: "worker_event",
        data: {
          delegation_id: "d1",
          event: {
            type: "agent_message",
            data: {
              uuid: "worker-text",
              type: "assistant",
              message: { content: [{ type: "text", text: "worker output" }] },
            },
          },
        },
      }],
      workers: [{
        delegation_id: "d1",
        worker_session_id: "w1",
        worker_description: "Reviewer",
        panel_kind: "worker",
        is_new: false,
        instructions_preview: "",
        events: [],
      }],
    });
    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        orchestrationMode: "team",
      }),
    );

    expect(container.querySelector(".event-diagnostic")).toBeNull();
    expect(container.textContent ?? "").not.toContain("unknown event");
    expect(container.textContent ?? "").toContain("Reviewer");
    expect(container.textContent ?? "").toContain("worker output");

    unmount();
  });

  it("routes agent_message-wrapped worker events without diagnostics", () => {
    const message = makeAssistantMsg({
      id: "a",
      content: "",
      events: [
        {
          type: "agent_message",
          data: {
            type: "worker_start",
            data: {
              delegation_id: "d1",
              worker_session_id: "w1",
              worker_description: "Researcher",
              panel_kind: "worker",
              is_new: false,
              instructions_preview: "",
            },
          },
        },
        {
          type: "agent_message",
          data: {
            type: "worker_event",
            data: {
              delegation_id: "d1",
              event: {
                type: "agent_message",
                data: {
                  uuid: "worker-text",
                  type: "assistant",
                  message: { content: [{ type: "text", text: "wrapped worker output" }] },
                },
              },
            },
          },
        },
        {
          type: "agent_message",
          data: {
            type: "worker_complete",
            data: {
              delegation_id: "d1",
              worker_session_id: "w1",
              success: true,
            },
          },
        },
      ],
      workers: [{
        delegation_id: "d1",
        worker_session_id: "w1",
        worker_description: "Researcher",
        panel_kind: "worker",
        is_new: false,
        instructions_preview: "",
        events: [],
      }],
    });
    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        orchestrationMode: "team",
      }),
    );

    expect(container.querySelector(".event-diagnostic")).toBeNull();
    expect(container.textContent ?? "").not.toContain("unknown event");
    expect(container.textContent ?? "").toContain("Researcher");
    expect(container.textContent ?? "").toContain("wrapped worker output");

    unmount();
  });

  it("routes raw Codex event.worker_event envelopes without diagnostics", () => {
    const message = makeAssistantMsg({
      id: "a",
      content: "",
      events: [{
        type: "agent_message",
        data: {
          type: "event",
          message: {
            type: "worker_event",
            payload: {
              delegation_id: "d1",
              event: {
                type: "agent_message",
                data: {
                  uuid: "worker-text",
                  type: "assistant",
                  message: { content: [{ type: "text", text: "worker output" }] },
                },
              },
            },
          },
        },
      }],
      workers: [{
        delegation_id: "d1",
        worker_session_id: "w1",
        worker_description: "Researcher",
        panel_kind: "worker",
        is_new: false,
        instructions_preview: "",
        events: [],
      }],
    });
    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        orchestrationMode: "team",
      }),
    );

    expect(container.querySelector(".event-diagnostic")).toBeNull();
    expect(container.textContent ?? "").not.toContain("unknown event");
    expect(container.textContent ?? "").not.toContain("event.worker_event");
    expect(container.textContent ?? "").toContain("Researcher");
    expect(container.textContent ?? "").toContain("worker output");

    unmount();
  });

  it("renders outer worker_event rows that reach a worker panel", () => {
    const message = makeAssistantMsg({
      id: "a",
      content: "",
      events: [],
      workers: [{
        delegation_id: "d1",
        worker_session_id: "w1",
        worker_description: "Researcher",
        panel_kind: "worker",
        is_new: false,
        instructions_preview: "",
        events: [{
          type: "worker_event",
          data: {
            delegation_id: "d1",
            event: {
              type: "agent_message",
              data: {
                uuid: "worker-text",
                type: "assistant",
                message: { content: [{ type: "text", text: "panel worker output" }] },
              },
            },
          },
        }],
      }],
    });
    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        orchestrationMode: "team",
      }),
    );

    expect(container.querySelector(".event-diagnostic")).toBeNull();
    expect(container.textContent ?? "").not.toContain("unknown event");
    expect(container.textContent ?? "").not.toContain("event.worker_event");
    expect(container.textContent ?? "").toContain("Researcher");
    expect(container.textContent ?? "").toContain("panel worker output");

    unmount();
  });

  it("unwraps nested agent_message events without diagnostics", () => {
    const message = makeAssistantMsg({
      id: "a",
      content: "",
      events: [{
        type: "agent_message",
        data: {
          type: "agent_message",
          data: {
            uuid: "nested-assistant",
            type: "assistant",
            message: {
              content: [{ type: "text", text: "nested assistant output" }],
            },
          },
        },
      }],
    });
    const { container, unmount } = render(
      React.createElement(MessageBubble, {
        message,
        orchestrationMode: "native",
      }),
    );

    expect(container.querySelector(".event-diagnostic")).toBeNull();
    expect(container.textContent ?? "").not.toContain("unknown event");
    expect(container.textContent ?? "").toContain("nested assistant output");

    unmount();
  });
});
