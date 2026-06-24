import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";
import {
  mergeEventlessMessageDelta,
  mergeIncomingMessageSnapshot,
} from "../src/hooks/useSession";

function textEvent(uuid: string, text: string) {
  return {
    type: "agent_message" as const,
    data: {
      uuid,
      type: "assistant",
      message: {
        content: [{ type: "text", text }],
      },
    },
  };
}

describe("messages_replay / messages_delta upsert + since_seq cursor", () => {
  it("subscribe frame includes since_seq=0 on first session select", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const sub = h.outbound.find(
      (f) => f.type === "subscribe" && f.app_session_id === session.id,
    );
    expect(sub).toBeDefined();
    expect(sub).toMatchObject({ since_seq: 0 });
    h.unmount();
  });

  it("messages_replay populates the chat from cold (no prior REST messages)", async () => {
    const session = makeSession({ messages: [] });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "messages_replay",
      data: {
        app_session_id: session.id,
        messages: [
          makeUserMsg({ id: "u", content: "hi", seq: 0 }),
          makeAssistantMsg({
            id: "a",
            content: "hello",
            seq: 1,
            events: [textEvent("ev-a-hello", "hello")],
            completed_at: "2026-06-19T21:44:17.000000",
          }),
        ],
      },
    });
    await h.flush();

    const view = h.toJSON();
    expect(view.chat.messages.find((m) => m.id === "u")?.text).toContain("hi");
    expect(h.raw.container.textContent).toContain("hello");
    h.unmount();
  });

  it("messages_replay upserts existing messages by id (replace, not duplicate)", async () => {
    const session = makeSession({
      messages: [
        makeUserMsg({ id: "u", content: "old user", seq: 0 }),
        makeAssistantMsg({
          id: "a",
          content: "old assistant",
          seq: 1,
          events: [textEvent("ev-a-old", "old assistant")],
        }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    // Replay with updated assistant content (live in-flight snapshot).
    h.emit({
      type: "messages_replay",
      data: {
        app_session_id: session.id,
        messages: [
          makeAssistantMsg({
            id: "a",
            content: "fresh assistant",
            seq: 1,
            events: [textEvent("ev-a-fresh", "fresh assistant")],
            completed_at: "2026-06-19T21:44:18.000000",
          }),
        ],
      },
    });
    await h.flush();

    const view = h.toJSON();
    expect(view.chat.messages.filter((m) => m.id === "u")).toHaveLength(1);
    expect(h.raw.container.textContent).toContain("fresh assistant");
    h.unmount();
  });

  it("messages_delta appends a newly-born assistant message", async () => {
    const session = makeSession({
      messages: [makeUserMsg({ id: "u", content: "hi", seq: 0 })],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "messages_delta",
      data: {
        app_session_id: session.id,
        messages: [
          makeAssistantMsg({
            id: "a-lazy",
            content: "",
            seq: 1,
            isStreaming: true,
          }),
        ],
      },
    });
    await h.flush();

    const view = h.toJSON();
    expect(view.chat.messages.map((m) => m.id)).toContain("a-lazy");
    h.unmount();
  });

  it("compact messages_delta updates finalized fields without dropping streamed events", () => {
    const streamedEvent = {
      type: "agent_message" as const,
      data: { uuid: "ev-1", type: "assistant" },
    };
    const workerEvent = {
      type: "agent_message" as const,
      data: { uuid: "worker-ev-1", type: "assistant" },
    };
    const current = makeAssistantMsg({
      id: "a",
      content: "partial",
      seq: 1,
      isStreaming: true,
      events: [streamedEvent],
      workers: [{
        delegation_id: "d1",
        worker_session_id: "w1",
        worker_description: "worker",
        events: [workerEvent],
      }],
    });
    const compact = makeAssistantMsg({
      id: "a",
      content: "partial complete",
      seq: 1,
      isStreaming: false,
      events: undefined,
      event_payload_omitted: true,
      workers: [{
        delegation_id: "d1",
        worker_session_id: "w1",
        worker_description: "worker",
        success: true,
      } as never],
    });

    const msg = mergeEventlessMessageDelta(current, compact);
    expect(msg.content).toBe("partial complete");
    expect(msg.isStreaming).toBe(false);
    expect(msg.events).toEqual([streamedEvent]);
    expect(msg.workers?.[0].success).toBe(true);
    expect(msg.workers?.[0].events).toEqual([workerEvent]);
  });

  it("compact terminal messages_delta replaces streaming content even when text does not extend", () => {
    const streamedEvent = {
      type: "agent_message" as const,
      data: { uuid: "ev-1", type: "assistant" },
    };
    const current = makeAssistantMsg({
      id: "a",
      content: "tool chatter",
      seq: 1,
      isStreaming: true,
      events: [streamedEvent],
    });
    const compact = makeAssistantMsg({
      id: "a",
      content: "final answer",
      seq: 1,
      isStreaming: false,
      events: undefined,
      event_payload_omitted: true,
      completed_at: "2026-06-19T21:44:16.000000",
    });

    const msg = mergeIncomingMessageSnapshot(current, compact);
    expect(msg?.content).toBe("final answer");
    expect(msg?.isStreaming).toBe(false);
    expect(msg?.events).toEqual([streamedEvent]);
  });

  it("terminal replay replaces an empty streaming placeholder with zero events", () => {
    const current = makeAssistantMsg({
      id: "a",
      content: "",
      seq: 1,
      isStreaming: true,
      events: [],
    });
    const incoming = makeAssistantMsg({
      id: "a",
      content: "final answer",
      seq: 1,
      isStreaming: false,
      events: [],
      completed_at: "2026-06-19T21:44:17.687329",
    });

    const msg = mergeIncomingMessageSnapshot(current, incoming);
    expect(msg?.content).toBe("final answer");
    expect(msg?.isStreaming).toBe(false);
    expect(msg?.completed_at).toBe("2026-06-19T21:44:17.687329");
  });

  it("zero-event replay without terminal marker does not replace empty streaming placeholder", () => {
    const current = makeAssistantMsg({
      id: "a",
      content: "",
      seq: 1,
      isStreaming: true,
      events: [],
    });
    const incoming = makeAssistantMsg({
      id: "a",
      content: "stale answer",
      seq: 1,
      isStreaming: false,
      events: [],
    });

    expect(mergeIncomingMessageSnapshot(current, incoming)).toBeNull();
  });

  it("cold REST select seeds the cursor from the highest seq in the payload", async () => {
    const a = makeSession({
      id: "a",
      messages: [
        makeUserMsg({ id: "u", seq: 0 }),
        makeAssistantMsg({ id: "as", seq: 1 }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [a] } });
    await h.selectSession("a");

    const subA = h.outbound.find(
      (f) => f.type === "subscribe" && f.app_session_id === "a",
    );
    // Highest seq in REST payload is 1, so the very first subscribe
    // sends since_seq=1 (the backend's `seq >= since_seq` filter
    // re-emits the in-flight assistant on reconnect, idempotently).
    expect(subA?.since_seq).toBe(1);
    h.unmount();
  });

  it("REST-seeded session with no seq metadata sends since_seq=0", async () => {
    const a = makeSession({
      id: "a",
      // Legacy messages with no seq field should keep cursor at 0.
      messages: [
        makeUserMsg({ id: "u" }),
        makeAssistantMsg({ id: "as" }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [a] } });
    await h.selectSession("a");

    const subA = h.outbound.find(
      (f) => f.type === "subscribe" && f.app_session_id === "a",
    );
    expect(subA?.since_seq).toBe(0);
    h.unmount();
  });

  it("messages_replay sorts merged messages by seq", async () => {
    const session = makeSession({ messages: [] });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    // Replay arrives out of order.
    h.emit({
      type: "messages_replay",
      data: {
        app_session_id: session.id,
        messages: [
          makeAssistantMsg({
            id: "a",
            content: "second",
            seq: 3,
            events: [textEvent("ev-a-second", "second")],
            completed_at: "2026-06-19T21:44:19.000000",
          }),
          makeUserMsg({ id: "u1", content: "first", seq: 2 }),
        ],
      },
    });
    await h.flush();

    expect(h.toJSON().chat.messages.map((m) => m.id)).toEqual(["u1"]);
    expect(h.raw.container.textContent).toContain("second");
    h.unmount();
  });

  it("rewind_complete REPLACES messages and shrinks the visible list", async () => {
    const session = makeSession({
      messages: [
        makeUserMsg({ id: "u1", seq: 0 }),
        makeAssistantMsg({ id: "a1", seq: 1 }),
        makeUserMsg({ id: "u2", seq: 2 }),
        makeAssistantMsg({ id: "a2", seq: 3 }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    // 4 turns of messages → 2 user bubbles visible (assistants of
    // collapsed prior turns aren't rendered).
    expect(
      h.toJSON().chat.messages.filter((m) => m.role === "user").map((m) => m.id),
    ).toEqual(["u1", "u2"]);

    h.emit({
      type: "rewind_complete",
      session_id: session.id,
      messages: [
        makeUserMsg({ id: "u1", seq: 0 }),
        makeAssistantMsg({ id: "a1", seq: 1 }),
      ],
    } as unknown as Parameters<typeof h.emit>[0]);
    await h.flush();

    expect(
      h.toJSON().chat.messages.filter((m) => m.role === "user").map((m) => m.id),
    ).toEqual(["u1"]);
    h.unmount();
  });
});
