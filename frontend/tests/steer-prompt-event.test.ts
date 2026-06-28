import { describe, expect, it } from "vitest";
import { fireEvent } from "@testing-library/react";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeRun, makeSession, makeUserMsg } from "./fixtures";
import { buildInlineTagsPreamble } from "../src/utils/inlineTagsPrompt";
import type { InlineTag } from "../src/types/inlineTag";

async function typeAndSteer(h: Awaited<ReturnType<typeof renderApp>>, text: string) {
  const input = h.$('[data-testid="input-textarea"]') as HTMLTextAreaElement | null;
  if (!input) throw new Error("input textarea not present");
  fireEvent.change(input, { target: { value: text } });
  for (let i = 0; i < 10 && !h.$('[data-testid="steer-btn"]'); i++) {
    await h.flush();
  }
  await h.click('[data-testid="steer-btn"]');
}

async function waitForNoSendingMessages(h: Awaited<ReturnType<typeof renderApp>>) {
  for (let i = 0; i < 10; i++) {
    const messages = h.toJSON().chat.messages;
    if (!messages.some((m) => m.role === "user" && m.status === "sending")) {
      return messages;
    }
    await h.flush();
  }
  return h.toJSON().chat.messages;
}

async function waitForOutboundSend(h: Awaited<ReturnType<typeof renderApp>>, prompt: string) {
  for (let i = 0; i < 10; i++) {
    const sent = h.outbound.find((f) => f.type === "send_message" && f.prompt === prompt);
    if (sent) return sent;
    await h.flush();
  }
  return h.outbound.find((f) => f.type === "send_message" && f.prompt === prompt);
}

describe("steer prompt events", () => {
  it("queues the active draft without consuming an existing queued prompt", async () => {
    const session = makeSession({
      provider_id: "codex",
      messages: [
        makeUserMsg({ id: "u1", content: "start work" }),
        makeAssistantMsg({ id: "a1", isStreaming: true }),
      ],
      queued_prompts: [{
        id: "q1",
        content: "queued steer",
        client_id: "client-q1",
      }],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    expect(h.$('[data-testid="queued-prompt-banner"]')?.textContent).toContain("queued steer");
    await h.typeAndSend("draft steer");

    expect(h.outbound.find((frame) => frame.type === "send_message")).toMatchObject({
      type: "send_message",
      app_session_id: session.id,
      send_mode: "queue",
    });
    expect(h.$('[data-testid="queued-prompt-banner"]')?.textContent).toContain("queued steer");
  });

  it("renders the steer prompt inside the assistant turn and removes the optimistic user bubble", async () => {
    const userMessage = makeUserMsg({ id: "u1", content: "start work" });
    const assistantMessage = makeAssistantMsg({
      id: "a1",
      isStreaming: true,
    });
    const session = makeSession({
      provider_id: "codex",
      messages: [userMessage, assistantMessage],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    h.emit({
      type: "run_state",
      data: { app_session_id: session.id, runs: [makeRun({ target_message_id: "a1" })] },
    });
    await h.flush();

    await typeAndSteer(h, "steer inside turn");
    const sent = await waitForOutboundSend(h, "steer inside turn");
    expect(sent).toMatchObject({ send_mode: "steer" });
    const clientId = sent!.client_id as string;

    h.emit({
      type: "steer_prompt",
      data: {
        app_session_id: session.id,
        uuid: "steer-1",
        prompt: "steer inside turn",
      },
    });
    h.emit({
      type: "steer_prompt_persisted",
      data: {
        app_session_id: session.id,
        client_id: clientId,
      },
    });
    await h.flush();

    const messages = await waitForNoSendingMessages(h);
    expect(messages.filter((m) => m.role === "user")).toHaveLength(1);
    expect(messages.some((m) => m.role === "user" && m.status === "sending")).toBe(false);
  });

  it("renders inline tags in steer prompts as comment cards", async () => {
    const tag: InlineTag = {
      id: "t1",
      messageId: "u1",
      selectedText: "selected code",
      comment: "tighten this",
      timestamp: "2026-06-15T10:00:00.000Z",
    };
    const prompt = buildInlineTagsPreamble([tag]) + "\nApply the note.";
    const session = makeSession({
      provider_id: "codex",
      messages: [
        makeUserMsg({ id: "u1", content: "start work" }),
        makeAssistantMsg({
          id: "a1",
          isStreaming: true,
          events: [{
            type: "steer_prompt",
            data: {
              app_session_id: "sess-1",
              uuid: "steer-1",
              prompt,
            },
          }],
        }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const assistant = h.toJSON().chat.messages.find((m) => m.role === "assistant");
    expect(assistant?.text).toContain("Inline tags");
    expect(assistant?.text).toContain("selected code");
    expect(assistant?.text).toContain("tighten this");
    expect(assistant?.text).toContain("Apply the note.");
    expect(assistant?.text).not.toContain("<inline-tags>");

    h.unmount();
  });

  it("treats turn_start as active before run_state arrives", async () => {
    const session = makeSession({
      provider_id: "codex",
      messages: [
        makeUserMsg({ id: "u1", content: "start work" }),
        makeAssistantMsg({ id: "a1", isStreaming: true }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({ type: "turn_start", data: { app_session_id: session.id } });
    await h.flush();
    await typeAndSteer(h, "steer before run_state");

    const sent = await waitForOutboundSend(h, "steer before run_state");
    expect(sent).toMatchObject({
      send_mode: "steer",
    });
  });
});
