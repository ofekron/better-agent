import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";

/**
 * Regression: when a messages_replay arrives mid-stream, it must NOT
 * overwrite the live streaming assistant message with stale content
 * from a previous turn. Before the fix, the replay's `content` field
 * (populated for the completed previous turn) would appear as the
 * new assistant's text.
 */
describe("stale content from replay during streaming", () => {
  const PREV_TEXT = "Previous response text that should not leak";

  async function setup() {
    const userMsg1 = makeUserMsg({ content: "first prompt", seq: 0 });
    const asstMsg1 = makeAssistantMsg({
      id: "asst-prev",
      content: PREV_TEXT,
      seq: 1,
      isStreaming: false,
    });
    const session = makeSession({
      messages: [userMsg1, asstMsg1],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    return { h, sessionId: session.id };
  }

  it("replay with stale content does not overwrite streaming assistant", async () => {
    const { h, sessionId } = await setup();

    // User sends second prompt
    await h.typeAndSend("second prompt");
    await h.flush();

    // Backend creates new assistant message (streaming)
    h.emit({
      type: "messages_delta",
      data: {
        app_session_id: sessionId,
        messages: [
          makeAssistantMsg({
            id: "asst-new",
            content: "",
            isStreaming: true,
            seq: 3,
          }),
        ],
      },
    });
    await h.flush();

    // Simulate a stale replay arriving mid-stream — includes the NEW
    // assistant message with stale content and isStreaming=false.
    h.emit({
      type: "messages_replay",
      data: {
        app_session_id: sessionId,
        since_seq: 0,
        next_seq: 10,
        messages: [
          makeAssistantMsg({
            id: "asst-new",
            content: PREV_TEXT, // STALE: previous turn's text on new msg
            isStreaming: false, // STALE: backend snapshot says not streaming
            seq: 3,
          }),
        ],
      },
    });
    await h.flush();

    // The streaming assistant must NOT show the stale text.
    // Find the last assistant message in the view.
    const view = h.toJSON();
    const messages = view.chat?.messages ?? [];
    const asstMessages = messages.filter((m) => m.role === "assistant");
    const lastAsst = asstMessages[asstMessages.length - 1];

    expect(lastAsst).toBeDefined();
    expect(lastAsst.text).not.toContain(PREV_TEXT);

    h.unmount();
  });

  it("finalized replay that extends live text replaces streaming assistant", async () => {
    const { h, sessionId } = await setup();

    await h.typeAndSend("second prompt");
    await h.flush();

    h.emit({
      type: "messages_delta",
      data: {
        app_session_id: sessionId,
        messages: [
          makeAssistantMsg({
            id: "asst-new",
            content: "partial",
            isStreaming: true,
            seq: 3,
          }),
        ],
      },
    });
    await h.flush();

    h.emit({
      type: "messages_replay",
      data: {
        app_session_id: sessionId,
        since_seq: 3,
        next_seq: 4,
        messages: [
          makeAssistantMsg({
            id: "asst-new",
            content: "partial complete",
            events: [{
              type: "agent_message",
              data: {
                uuid: "replay-complete",
                type: "assistant",
                message: {
                  content: [{ type: "text", text: "partial complete" }],
                },
              },
            }],
            isStreaming: false,
            seq: 3,
          }),
        ],
      },
    });
    await h.flush();

    expect(h.raw.container.textContent).toContain("partial complete");

    h.unmount();
  });
});
