import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";
import {
  mergeProjectedMessageDelta,
  mergeIncomingMessagesForNode,
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

async function waitFor(
  h: Awaited<ReturnType<typeof renderApp>>,
  predicate: () => boolean,
) {
  for (let i = 0; i < 20; i++) {
    if (predicate()) return true;
    await h.flush();
  }
  return false;
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
    await waitFor(
      h,
      () => h.raw.container.textContent?.includes("fresh assistant") === true,
    );

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

  it("messages_delta adopts an early live placeholder into the canonical assistant", () => {
    const merged = mergeIncomingMessagesForNode(
      [
        makeUserMsg({ id: "u", content: "hi", seq: 0 }),
        makeAssistantMsg({
          id: "live-early",
          seq: undefined,
          isStreaming: true,
          events: [textEvent("early-live", "streamed before delta")],
        }),
      ],
      [
        makeAssistantMsg({
          id: "a-canonical",
          content: "",
          seq: 1,
          isStreaming: true,
          events: [],
        }),
      ],
    );

    expect(merged.map((m) => m.id)).toEqual(["u", "a-canonical"]);
    const canonical = merged.find((m) => m.id === "a-canonical");
    expect(canonical?.events).toEqual([textEvent("early-live", "streamed before delta")]);
  });

  it("messages_replay removes an adopted live placeholder when canonical already has the same uuid", () => {
    const merged = mergeIncomingMessagesForNode(
      [
        makeUserMsg({ id: "u", content: "hi", seq: 0 }),
        makeAssistantMsg({
          id: "live-early",
          seq: undefined,
          isStreaming: true,
          events: [textEvent("same-live", "partial text")],
        }),
      ],
      [
        makeAssistantMsg({
          id: "a-canonical",
          content: "final text",
          seq: 1,
          events: [textEvent("same-live", "final text")],
          completed_at: "2026-06-29T20:00:00.000000",
        }),
      ],
    );

    expect(merged.map((m) => m.id)).toEqual(["u", "a-canonical"]);
    const canonical = merged.find((m) => m.id === "a-canonical");
    expect(canonical?.events).toEqual([textEvent("same-live", "final text")]);
  });

  it("placeholder adoption keeps adjacent prompt groups isolated", () => {
    const merged = mergeIncomingMessagesForNode(
      [
        makeUserMsg({ id: "u1", content: "first", seq: 0 }),
        makeUserMsg({ id: "u2", content: "second", seq: 2 }),
        makeAssistantMsg({
          id: "live-second",
          seq: undefined,
          isStreaming: true,
          events: [textEvent("second-live", "belongs to second")],
        }),
      ],
      [
        makeAssistantMsg({
          id: "a-first",
          content: "",
          seq: 1,
          isStreaming: true,
          events: [],
        }),
      ],
    );

    expect(merged.map((m) => m.id)).toEqual(["u1", "a-first", "u2", "live-second"]);
    expect(merged.find((m) => m.id === "a-first")?.events).toEqual([]);
    expect(merged.find((m) => m.id === "live-second")?.events).toEqual([
      textEvent("second-live", "belongs to second"),
    ]);
  });

  it("placeholder adoption preserves worker panel events on the canonical assistant", () => {
    const workerEvent = textEvent("worker-live", "worker streamed early");
    const merged = mergeIncomingMessagesForNode(
      [
        makeUserMsg({ id: "u", content: "hi", seq: 0 }),
        makeAssistantMsg({
          id: "live-worker",
          seq: undefined,
          isStreaming: true,
          workers: [{
            delegation_id: "d1",
            worker_session_id: "w1",
            worker_description: "worker",
            events: [workerEvent],
          }],
        }),
      ],
      [
        makeAssistantMsg({
          id: "a-canonical",
          content: "",
          seq: 1,
          isStreaming: true,
          events: [],
        }),
      ],
    );

    expect(merged.map((m) => m.id)).toEqual(["u", "a-canonical"]);
    expect(merged.find((m) => m.id === "a-canonical")?.workers?.[0]).toMatchObject({
      delegation_id: "d1",
      events: [workerEvent],
    });
  });

  it("placeholder adoption normalizes missing event arrays to empty arrays", () => {
    const merged = mergeIncomingMessagesForNode(
      [
        makeUserMsg({ id: "u", content: "hi", seq: 0 }),
        makeAssistantMsg({
          id: "live-worker",
          seq: undefined,
          isStreaming: true,
          workers: [{
            delegation_id: "d1",
            worker_session_id: "w1",
            worker_description: "worker",
            events: undefined,
          }],
        }),
      ],
      [
        makeAssistantMsg({
          id: "a-canonical",
          content: "",
          seq: 1,
          isStreaming: true,
          events: undefined,
          workers: [{
            delegation_id: "d1",
            worker_session_id: "w1",
            worker_description: "worker",
            events: undefined,
          }],
        }),
      ],
    );

    const canonical = merged.find((m) => m.id === "a-canonical");
    expect(canonical?.events).toEqual([]);
    expect(canonical?.workers?.[0].events).toEqual([]);
  });

  it("placeholder adoption ignores empty seq-less canonical snapshots without overlap", () => {
    const merged = mergeIncomingMessagesForNode(
      [
        makeUserMsg({ id: "u", content: "hi", seq: 0 }),
        makeAssistantMsg({
          id: "live-unrelated",
          seq: undefined,
          isStreaming: true,
          events: [textEvent("unrelated-live", "unrelated")],
        }),
      ],
      [
        makeAssistantMsg({
          id: "a-seq-less",
          content: "",
          seq: undefined,
          isStreaming: true,
          events: [],
        }),
      ],
    );

    expect(merged.map((m) => m.id)).toEqual(["u", "live-unrelated", "a-seq-less"]);
    expect(merged.find((m) => m.id === "a-seq-less")?.events).toEqual([]);
  });

  it("placeholder adoption does not guess when one prompt group has multiple live placeholders", () => {
    const merged = mergeIncomingMessagesForNode(
      [
        makeUserMsg({ id: "u", content: "hi", seq: 0 }),
        makeAssistantMsg({
          id: "live-one",
          seq: undefined,
          isStreaming: true,
          events: [textEvent("live-one", "one")],
        }),
        makeAssistantMsg({
          id: "live-two",
          seq: undefined,
          isStreaming: true,
          events: [textEvent("live-two", "two")],
        }),
      ],
      [
        makeAssistantMsg({
          id: "a-canonical",
          content: "",
          seq: 1,
          isStreaming: true,
          events: [],
        }),
      ],
    );

    expect(merged.map((m) => m.id)).toEqual(["u", "a-canonical", "live-one", "live-two"]);
    expect(merged.find((m) => m.id === "a-canonical")?.events).toEqual([]);
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
      omitted_payloads: { events: { revision: "rev-1" } },
      workers: [{
        delegation_id: "d1",
        worker_session_id: "w1",
        worker_description: "worker",
        success: true,
      } as never],
    });

    const msg = mergeProjectedMessageDelta(current, compact);
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
      omitted_payloads: { events: { revision: "rev-1" } },
      completed_at: "2026-06-19T21:44:16.000000",
    });

    const msg = mergeIncomingMessageSnapshot(current, compact);
    expect(msg?.content).toBe("final answer");
    expect(msg?.isStreaming).toBe(false);
    expect(msg?.events).toEqual([streamedEvent]);
  });

  it("in-flight compact messages_delta updates a streaming message instead of being dropped", () => {
    const streamedEvent = {
      type: "agent_message" as const,
      data: { uuid: "ev-1", type: "assistant" },
    };
    const current = makeAssistantMsg({
      id: "a",
      content: "lead-in",
      seq: 1,
      isStreaming: true,
      events: [streamedEvent],
    });
    const compact = makeAssistantMsg({
      id: "a",
      content: "final answer",
      seq: 1,
      isStreaming: true,
      events: undefined,
      omitted_payloads: { events: { revision: "rev-2", count: 1 } },
    });

    const msg = mergeIncomingMessageSnapshot(current, compact);
    expect(msg?.content).toBe("final answer");
    expect(msg?.events).toEqual([streamedEvent]);
  });

  it("a collapsed finished turn shows the final answer, not the lead-in it opened with", async () => {
    const session = makeSession({
      messages: [
        makeUserMsg({ id: "u1", content: "is it safe?", seq: 0 }),
        makeAssistantMsg({
          id: "a1",
          content: "LEAD IN TEXT",
          seq: 1,
          isStreaming: true,
          events: [textEvent("ev-lead", "LEAD IN TEXT")],
        }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    // In-flight compacted deltas — the only carrier of the turn's content
    // while it streams. Events are omitted, never zero-length.
    h.emit({
      type: "messages_delta",
      data: {
        app_session_id: session.id,
        messages: [
          {
            ...makeAssistantMsg({
              id: "a1",
              content: "FINAL ANSWER TEXT",
              seq: 1,
              isStreaming: true,
              omitted_payloads: { events: { revision: "rev-9", count: 2 } },
            }),
            events: undefined,
          },
        ],
      },
    });
    h.emit({
      type: "turn_complete",
      data: { session_id: session.id, success: true },
    });
    await h.flush();

    await waitFor(
      h,
      () => h.raw.container.textContent?.includes("FINAL ANSWER TEXT") === true,
    );
    expect(h.raw.container.textContent).not.toContain("LEAD IN TEXT");
    h.unmount();
  });

  it("compact messages_delta keeps the transient isRecovering flag", () => {
    const current = makeAssistantMsg({
      id: "a",
      content: "lead-in",
      seq: 1,
      isStreaming: true,
      isRecovering: true,
      events: [],
    });
    const compact = makeAssistantMsg({
      id: "a",
      content: "later text",
      seq: 1,
      isStreaming: true,
      events: undefined,
      omitted_payloads: { events: { revision: "rev-3", count: 2 } },
    });

    const msg = mergeIncomingMessageSnapshot(current, compact);
    expect(msg?.content).toBe("later text");
    expect(msg?.isRecovering).toBe(true);
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
    await waitFor(h, () =>
      h.outbound.some(
        (frame) =>
          frame.type === "subscribe" &&
          frame.app_session_id === "a" &&
          frame.since_seq === 1,
      ),
    );

    const subA = h.outbound.filter(
      (f) => f.type === "subscribe" && f.app_session_id === "a",
    ).at(-1);
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

  it("reconnect replaces a completed partial tree before resubscribing", async () => {
    const visiblePrompt = makeUserMsg({
      id: "u3",
      content: "visible cached prompt",
      seq: 4,
      timestamp: "2026-06-19T21:44:19.000000",
    });
    const partialAssistant = makeAssistantMsg({
      id: "a3",
      content: "partial failure",
      seq: 5,
      isStreaming: true,
      timestamp: "2026-06-19T21:44:20.000000",
    });
    const partial = makeSession({
      id: "a",
      name: "partial",
      messages: [visiblePrompt, partialAssistant],
    });
    const other = makeSession({ id: "b", name: "other" });
    const h = await renderApp({ seed: { sessions: [partial, other] } });
    await h.selectSession(partial.id);

    h.emit({
      type: "turn_complete",
      data: { app_session_id: partial.id, success: true },
    });
    await h.flush();
    // Completed (non-live) turns default to collapsed — expand to see
    // the assistant message as a rendered element.
    await h.expandTurn("u3");
    expect(h.toJSON().chat.messages.map((message) => message.id)).toEqual(["u3", "a3"]);
    expect(h.raw.container.textContent).toContain("partial failure");

    const authoritative = makeSession({
      ...partial,
      messages: [
        makeUserMsg({
          id: "u1",
          content: "first missing prompt",
          seq: 0,
          timestamp: "2026-06-19T21:44:15.000000",
        }),
        makeAssistantMsg({
          id: "a1",
          content: "first answer",
          seq: 1,
          timestamp: "2026-06-19T21:44:16.000000",
          completed_at: "2026-06-19T21:44:17.000000",
        }),
        makeUserMsg({
          id: "u2",
          content: "second missing prompt",
          seq: 2,
          timestamp: "2026-06-19T21:44:17.000000",
        }),
        makeAssistantMsg({
          id: "a2",
          content: "second answer",
          seq: 3,
          timestamp: "2026-06-19T21:44:18.000000",
          completed_at: "2026-06-19T21:44:18.000000",
        }),
        visiblePrompt,
        {
          ...partialAssistant,
          content: "canonical failure",
          isStreaming: false,
          completed_at: "2026-06-19T21:44:19.000000",
        },
      ],
    });
    h.backend.state.sessions[
      h.backend.state.sessions.findIndex((session) => session.id === partial.id)
    ] = authoritative;

    h.dropConnection();
    h.reopenConnection();
    // User messages always render regardless of collapse — wait on those
    // first, then expand the newly-arrived turns to see their answers.
    await waitFor(h, () =>
      h.toJSON().chat.messages
        .filter((message) => message.role === "user")
        .map((message) => message.id)
        .join(",") === "u1,u2,u3"
    );
    await h.expandTurn("u1");
    await h.expandTurn("u2");
    await waitFor(h, () =>
      h.toJSON().chat.messages.map((message) => message.id).join(",") ===
      "u1,a1,u2,a2,u3,a3"
    );
    const reconnectSubscribe = h.outbound
      .filter(
        (frame) =>
          frame.type === "subscribe" &&
          frame.app_session_id === partial.id,
      )
      .at(-1);
    expect(reconnectSubscribe).toMatchObject({ since_seq: 5 });

    expect(h.toJSON().chat.messages.map((message) => message.id)).toEqual([
      "u1",
      "a1",
      "u2",
      "a2",
      "u3",
      "a3",
    ]);
    expect(h.raw.container.textContent).toContain("first missing prompt");
    expect(h.raw.container.textContent).toContain("second missing prompt");
    expect(h.raw.container.textContent).toContain("canonical failure");
    expect(h.raw.container.textContent).not.toContain("partial failure");
    h.unmount();
  });

  it("messages_replay sorts merged messages by seq", async () => {
    const session = makeSession({ messages: [] });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await waitFor(h, () =>
      h.outbound.some(
        (frame) =>
          frame.type === "subscribe" &&
          frame.app_session_id === session.id,
      ),
    );

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
    await waitFor(
      h,
      () => h.raw.container.textContent?.includes("second") ?? false,
    );
    // Completed turns default to collapsed — expand to see "a" as a
    // rendered assistant-message element in chat.messages.
    await h.expandTurn("u1");

    expect(h.toJSON().chat.messages.map((m) => m.id)).toEqual(["u1", "a"]);
    const user = h.raw.container.querySelector(
      '[data-testid="user-message"][data-message-id="u1"]',
    );
    const response = h.raw.container.querySelector(
      '.turn-group-children[data-message-id="a"]',
    );
    expect(user).not.toBeNull();
    expect(response?.textContent).toContain("second");
    expect(
      user!.compareDocumentPosition(response!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
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
    await waitFor(
      h,
      () => h.toJSON().chat.messages.filter((m) => m.role === "user").map((m) => m.id).join(",") === "u1",
    );

    expect(
      h.toJSON().chat.messages.filter((m) => m.role === "user").map((m) => m.id),
    ).toEqual(["u1"]);
    h.unmount();
  });
});
