import { describe, expect, it } from "vitest";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";

describe("event cursor subscribe protocol", () => {
  it("sends known zero after REST loads a session with no event cursor entries", async () => {
    const session = makeSession({ messages: [] });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const sub = h.outbound.find(
      (f) => f.type === "subscribe" && f.app_session_id === session.id,
    );
    expect(sub).toMatchObject({
      events_from_seq: 0,
      events_cursor_known: true,
    });
    h.unmount();
  });

  it("sends REST event cursor with explicit knownness", async () => {
    const session = makeSession({
      id: "a",
      messages: [
        makeUserMsg({ id: "u", seq: 0 }),
        makeAssistantMsg({ id: "as", seq: 1 }),
      ],
      max_seq_by_sid: { a: 12 },
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession("a");

    const sub = h.outbound.find(
      (f) => f.type === "subscribe" && f.app_session_id === "a",
    );
    expect(sub).toMatchObject({
      events_from_seq: 12,
      events_cursor_known: true,
    });
    h.unmount();
  });
});
