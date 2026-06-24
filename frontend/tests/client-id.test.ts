import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeSession, makeUserMsg } from "./fixtures";
import { appendPendingUnlessAcked } from "../src/utils/pendingMessages";

/**
 * The optimistic pendingMessages list is keyed by session_id, and the
 * canonical user_message_persisted ack carries `client_id` (the pending
 * entry's id). The ack removes the matching pending entry only inside
 * the acked session.
 */
describe("client_id matching + per-session pending", () => {
  it("user_message_persisted with client_id removes the matching pending entry", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    await h.typeAndSend("first");

    const sends = h.outbound.filter((f) => f.type === "send_message");
    expect(sends).toHaveLength(1);
    const firstClientId = sends[0].client_id as string;

    expect(
      h.toJSON().chat.messages.filter((m) => m.status === "sending"),
    ).toHaveLength(1);

    h.emit({
      type: "user_message_persisted",
      data: {
        session_id: session.id,
        user_message: makeUserMsg({
          id: "u-first",
          content: "first",
          client_id: firstClientId,
          seq: 0,
        }),
      },
    });
    await h.flush();

    const messages = h.toJSON().chat.messages;
    expect(messages.filter((m) => m.status === "sending")).toHaveLength(0);
    expect(messages.find((m) => m.id === "u-first")?.text).toContain("first");
    h.unmount();
  });

  it("does not append pending when exact client_id was already acked", () => {
    const prev = [makeUserMsg({ id: "existing", content: "existing", seq: 0 })];
    const ackedClientIds = new Set(["pending-fast"]);
    const skipNextAppendBySession = new Set<string>();
    const next = appendPendingUnlessAcked(
      prev,
      "sess-1",
      makeUserMsg({ id: "pending-fast", content: "fast ack", seq: 1 }),
      { ackedClientIds, skipNextAppendBySession },
    );

    expect(next).toBe(prev);
  });

  it("does not append pending when legacy no-client ack already cleared the session", () => {
    const prev = [makeUserMsg({ id: "existing", content: "existing", seq: 0 })];
    const ackedClientIds = new Set<string>();
    const skipNextAppendBySession = new Set(["sess-1"]);
    const next = appendPendingUnlessAcked(
      prev,
      "sess-1",
      makeUserMsg({ id: "pending-legacy", content: "legacy fast ack", seq: 1 }),
      { ackedClientIds, skipNextAppendBySession },
    );

    expect(next).toBe(prev);
    expect(skipNextAppendBySession.has("sess-1")).toBe(false);
  });

  it("late send errors do not resurrect a failed pending entry after backend ack", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    await h.typeAndSend("accepted");
    const sent = h.outbound.find((f) => f.type === "send_message");
    const clientId = sent!.client_id as string;

    h.emit({
      type: "user_message_persisted",
      data: {
        session_id: session.id,
        user_message: makeUserMsg({
          id: "u-accepted",
          content: "accepted",
          client_id: clientId,
          seq: 0,
        }),
      },
    });
    await h.flush();

    h.emit({
      type: "error",
      data: {
        app_session_id: session.id,
        client_id: clientId,
        error: "late transport error",
      },
    });
    await h.flush();

    const messages = h.toJSON().chat.messages;
    expect(messages.filter((m) => m.id === clientId)).toHaveLength(0);
    expect(messages.some((m) => m.status === "error")).toBe(false);
    expect(messages.find((m) => m.id === "u-accepted")?.text).toContain("accepted");
    h.unmount();
  });

  it("pending entries are scoped per session — switching sessions hides the other's pending", async () => {
    const a = makeSession({ id: "a" });
    const b = makeSession({ id: "b", name: "B" });
    const h = await renderApp({ seed: { sessions: [a, b] } });

    await h.selectSession("a");
    await h.typeAndSend("on A");

    expect(
      h.toJSON().chat.messages.some((m) => m.status === "sending"),
    ).toBe(true);

    await h.selectSession("b");
    expect(
      h.toJSON().chat.messages.some((m) => m.status === "sending"),
    ).toBe(false);

    // Back to A — its pending is still there.
    await h.selectSession("a");
    expect(
      h.toJSON().chat.messages.some((m) => m.status === "sending"),
    ).toBe(true);
    h.unmount();
  });

  it("user_message_persisted for a different session does NOT touch the active pending", async () => {
    const a = makeSession({ id: "a" });
    const b = makeSession({ id: "b", name: "B" });
    const h = await renderApp({ seed: { sessions: [a, b] } });

    await h.selectSession("a");
    await h.typeAndSend("on A");
    const sentForA = h.outbound.find((f) => f.type === "send_message");
    const aClientId = sentForA!.client_id as string;

    // Stray ack for session B carrying A's client_id (paranoid: should be ignored).
    h.emit({
      type: "user_message_persisted",
      data: {
        session_id: "b",
        user_message: makeUserMsg({
          id: "u-stray",
          content: "stray",
          client_id: aClientId,
          seq: 0,
        }),
      },
    });
    await h.flush();

    // A's pending entry is still there because the ack targeted B.
    expect(
      h.toJSON().chat.messages.some((m) => m.status === "sending"),
    ).toBe(true);
    h.unmount();
  });

  it("send_message frames carry a unique client_id per send", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    await h.typeAndSend("a");
    await h.typeAndSend("b");
    await h.typeAndSend("c");

    const ids = h.outbound
      .filter((f) => f.type === "send_message")
      .map((f) => f.client_id);
    expect(new Set(ids).size).toBe(3);
    expect(ids.every((id) => typeof id === "string" && id.startsWith("pending-"))).toBe(true);
    h.unmount();
  });
});
