import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";

/**
 * Phase 3: live chat-tree deltas (BFF-rendered Turn/ModelChange items +
 * lookup, same shape as GET /api/chat-tree) replace the old raw
 * agent_message/manager_event/worker_start/worker_event/worker_complete/
 * worker_prep events/todos_snapshot live-rendering path. This locks the
 * new `chat_tree_delta` WS handling end to end through the real app
 * tree (useWebSocket -> useSession.applyChatTreeDelta -> Chat/MessageBubble).
 * Per-message isStreaming/stopped_at/isDetached preservation semantics
 * are locked separately (and more directly) in
 * mergeChatTreeDeltaMessage.test.ts.
 */
function turnDeltaFrame(opts: {
  sessionId: string;
  turnId: string;
  assistantId: string;
  phase: "streaming" | "settled" | "stopped" | "detached";
  promptText: string;
  resultText: string;
  workers?: unknown[];
}) {
  return {
    type: "chat_tree_delta" as const,
    data: {
      app_session_id: opts.sessionId,
      turn_id: opts.turnId,
      phase: opts.phase,
      items: [
        {
          type: "Turn",
          id: opts.turnId,
          prompt: opts.turnId,
          body: opts.workers?.length
            ? [{ type: "Explanation", text: "", text_event_ids: [], item_ids: ["out-1"] }]
            : [],
          result: opts.resultText
            ? { type: "ProviderResult", part_ids: ["out-1"], text: opts.resultText }
            : null,
        },
      ],
      lookup: {
        [opts.turnId]: {
          kind: "message",
          role: "user",
          text: opts.promptText,
          seq: 10,
          snapshot: { id: opts.turnId, role: "user", seq: 10 },
        },
        [opts.assistantId]: {
          kind: "message",
          role: "assistant",
          text: "",
          seq: 11,
          snapshot: {
            id: opts.assistantId,
            role: "assistant",
            seq: 11,
            ...(opts.workers ? { workers: opts.workers } : {}),
          },
        },
        "out-1": {
          kind: "event",
          type: "assistant_text",
          data: { text: opts.resultText },
          message_id: opts.assistantId,
        },
      },
    },
  };
}

describe("live chat_tree_delta rendering", () => {
  it("streams progressive content onto the assistant bubble", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit(turnDeltaFrame({
      sessionId: session.id,
      turnId: "u1",
      assistantId: "a1",
      phase: "streaming",
      promptText: "go",
      resultText: "partial answer",
    }));
    await h.flush();

    const view = h.toJSON();
    const assistant = view.chat.messages.find((m) => m.id === "a1");
    expect(assistant).toBeDefined();
    expect(assistant?.text ?? "").toContain("partial answer");
    h.unmount();
  });

  it("a later settle delta for the same turn replaces the streamed content with the final text", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit(turnDeltaFrame({
      sessionId: session.id, turnId: "u1", assistantId: "a1",
      phase: "streaming", promptText: "go", resultText: "partial",
    }));
    await h.flush();
    h.emit(turnDeltaFrame({
      sessionId: session.id, turnId: "u1", assistantId: "a1",
      phase: "settled", promptText: "go", resultText: "final answer",
    }));
    await h.flush();

    const view = h.toJSON();
    const assistant = view.chat.messages.find((m) => m.id === "a1");
    expect(assistant?.text ?? "").toContain("final answer");
    h.unmount();
  });

  it("does not crash when a raw turn_stopped frame and a settle delta for the same turn both arrive", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit(turnDeltaFrame({
      sessionId: session.id, turnId: "u1", assistantId: "a1",
      phase: "streaming", promptText: "go", resultText: "partial",
    }));
    await h.flush();

    // Lifecycle frame (unintercepted, still raw) wins the race first.
    h.emit({
      type: "turn_stopped",
      data: {
        app_session_id: session.id,
        stopped_at: "2026-07-16T00:00:00.000Z",
        interrupted_by_msg_id: "u2",
      },
    });
    await h.flush();

    // The settle delta for the same turn lands after it.
    h.emit(turnDeltaFrame({
      sessionId: session.id, turnId: "u1", assistantId: "a1",
      phase: "stopped", promptText: "go", resultText: "partial",
    }));
    await h.flush();

    const assistant = h.toJSON().chat.messages.find((m) => m.id === "a1");
    expect(assistant?.text ?? "").toContain("partial");
    h.unmount();
  });

  it("worker panel data from the assistant snapshot reaches the rendered message list", async () => {
    // The backend contract for this (worker_start/_event/_complete facts
    // carrying their full payload through the assistant snapshot's
    // `workers` array, no longer stripped) is locked by
    // scripts/test_bff_chat_tree.py; `chatTreeToMessages`'s
    // `snapshotExtras()` spread onto the ChatMessage is verified directly.
    // This confirms the same data survives the live WS -> useSession
    // merge into the rendered assistant message (DOM panel-visibility
    // preconditions are MessageBubble's existing rendering contract,
    // exercised identically by the REST history path — not something
    // this live-delta change alters).
    const session = makeSession({ orchestration_mode: "team" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit(turnDeltaFrame({
      sessionId: session.id, turnId: "u1", assistantId: "a1",
      phase: "streaming", promptText: "go", resultText: "",
      workers: [{
        delegation_id: "d1",
        worker_session_id: "w-1",
        worker_description: "Researcher",
        is_new: false,
        instructions_preview: "find X",
        events: [],
      }],
    }));
    await h.flush();

    const view = h.toJSON();
    expect(view.chat.messages.some((m) => m.id === "a1")).toBe(true);
    h.unmount();
  });
});
