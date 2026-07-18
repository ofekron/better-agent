import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeSession, makeUserMsg } from "./fixtures";
import { upsertPendingUnlessAcked } from "../src/utils/pendingMessages";

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
    const next = upsertPendingUnlessAcked(
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
    const next = upsertPendingUnlessAcked(
      prev,
      "sess-1",
      makeUserMsg({ id: "pending-legacy", content: "legacy fast ack", seq: 1 }),
      { ackedClientIds, skipNextAppendBySession },
    );

    expect(next).toBe(prev);
    expect(skipNextAppendBySession.has("sess-1")).toBe(false);
  });

  it("replaces an existing optimistic failure with one fresh retry", () => {
    const prev = [makeUserMsg({ id: "pending-failed", status: "error" })];
    const retry = makeUserMsg({ id: "pending-retry", status: "sending" });

    const next = upsertPendingUnlessAcked(
      prev,
      "sess-1",
      retry,
      { ackedClientIds: new Set(), skipNextAppendBySession: new Set() },
      "pending-failed",
    );

    expect(next).toEqual([retry]);
  });

  it("removes the old failure without appending when the retry ack wins the race", () => {
    const prev = [makeUserMsg({ id: "pending-failed", status: "error" })];
    const retry = makeUserMsg({ id: "pending-retry", status: "sending" });

    const next = upsertPendingUnlessAcked(
      prev,
      "sess-1",
      retry,
      {
        ackedClientIds: new Set(["pending-retry"]),
        skipNextAppendBySession: new Set(),
      },
      "pending-failed",
    );

    expect(next).toEqual([]);
  });

  it("removes the old failure without appending after a legacy retry ack", () => {
    const prev = [makeUserMsg({ id: "pending-failed", status: "error" })];
    const retry = makeUserMsg({ id: "pending-retry", status: "sending" });
    const skipNextAppendBySession = new Set(["sess-1"]);

    const next = upsertPendingUnlessAcked(
      prev,
      "sess-1",
      retry,
      { ackedClientIds: new Set(), skipNextAppendBySession },
      "pending-failed",
    );

    expect(next).toEqual([]);
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

  it("retrying a persisted failed prompt immediately appends a pending retry", async () => {
    const failed = makeUserMsg({
      id: "u-failed",
      content: "retry this persisted prompt",
      status: "error",
      errorText: "offline",
      seq: 0,
    });
    const session = makeSession({ messages: [failed] });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    await h.clickByText("Retry");

    const sent = h.outbound.find((frame) => frame.type === "send_message");
    expect(sent).toEqual(expect.objectContaining({
      prompt: "retry this persisted prompt",
      client_id: expect.stringMatching(/^pending-/),
    }));
    expect(h.toJSON().chat.messages.filter((message) => message.role === "user")).toEqual([
      expect.objectContaining({ id: "u-failed", status: "error" }),
      expect.objectContaining({ id: sent!.client_id, status: "sending" }),
    ]);

    h.emit({
      type: "user_message_persisted",
      data: {
        session_id: session.id,
        user_message: makeUserMsg({
          id: "u-retry",
          content: "retry this persisted prompt",
          client_id: sent!.client_id as string,
          seq: 1,
        }),
      },
    });
    await h.flush();

    const userMessages = h.toJSON().chat.messages.filter((message) => message.role === "user");
    expect(userMessages).toEqual([
      expect.objectContaining({ id: "u-failed" }),
      expect.objectContaining({ id: "u-retry" }),
    ]);
    expect(userMessages.filter((message) => message.id === sent!.client_id)).toHaveLength(0);
    h.unmount();
  });

  it("keeps a failed retry visible when its source was a persisted prompt", async () => {
    const session = makeSession({
      messages: [makeUserMsg({
        id: "u-persisted-failure",
        content: "retry while disconnected",
        status: "error",
        errorText: "offline",
        seq: 0,
      })],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    h.dropConnection();

    await h.clickByText("Retry");

    const userMessages = h.toJSON().chat.messages.filter((message) => message.role === "user");
    expect(userMessages).toEqual([
      expect.objectContaining({ id: "u-persisted-failure", status: "error" }),
      expect.objectContaining({
        id: expect.stringMatching(/^pending-/),
        status: "error",
      }),
    ]);
    h.unmount();
  });

  it("replaces an optimistic failed prompt when its retry cannot send", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.typeAndSend("retry optimistic failure");
    const firstSend = h.outbound.find((frame) => frame.type === "send_message")!;
    h.emit({
      type: "error",
      data: {
        app_session_id: session.id,
        client_id: firstSend.client_id,
        error: "offline",
      },
    });
    await h.flush();
    h.dropConnection();

    await h.clickByText("Retry");

    const userMessages = h.toJSON().chat.messages.filter((message) => message.role === "user");
    expect(userMessages).toHaveLength(1);
    expect(userMessages[0]).toEqual(expect.objectContaining({
      id: expect.stringMatching(/^pending-/),
      status: "error",
    }));
    expect(userMessages[0].id).not.toBe(firstSend.client_id);
    h.unmount();
  });
});
