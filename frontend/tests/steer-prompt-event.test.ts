import { describe, expect, it } from "vitest";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeRun, makeSession, makeUserMsg } from "./fixtures";
import { buildInlineTagsPreamble } from "../src/utils/inlineTagsPrompt";
import type { InlineTag } from "../src/types/inlineTag";

describe("steer prompt events", () => {
  it("keeps a queued prompt visible until queued-steer succeeds", async () => {
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
    await h.click('[data-testid="queued-steer-btn"]');

    expect(h.outbound.at(-1)).toMatchObject({
      type: "promote_queued",
      app_session_id: session.id,
      action: "steer",
    });
    expect(h.$('[data-testid="queued-prompt-banner"]')?.textContent).toContain("queued steer");

    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: session.id,
        patch: {
          queued_prompts: [],
        },
      },
    });
    await h.flush();

    expect(h.$('[data-testid="queued-prompt-banner"]')).toBeNull();
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
      data: { app_session_id: session.id, runs: [makeRun()] },
    });
    await h.flush();

    await h.typeAndSend("steer inside turn");
    const sent = h.outbound.find((f) => f.type === "send_message");
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

    const messages = h.toJSON().chat.messages;
    expect(messages).toHaveLength(2);
    expect(messages.filter((m) => m.role === "user")).toHaveLength(1);
    expect(messages.some((m) => m.role === "user" && m.status === "sending")).toBe(false);
    expect(messages.find((m) => m.role === "assistant")?.text).toContain("steer inside turn");
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
    await h.typeAndSend("steer before run_state");

    expect(h.outbound.find((f) => f.type === "send_message")).toMatchObject({
      send_mode: "steer",
    });
  });
});
