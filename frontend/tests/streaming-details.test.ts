import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeRun, makeSession, makeUserMsg } from "./fixtures";

/**
 * The synthetic streaming bubble is gone — live `manager_event` /
 * `worker_event` / etc. frames now apply to the canonical assistant
 * message in messages[]. Tests have to:
 *   1. Seed at least the user + lazily-born assistant via messages_replay
 *      (or messages_delta).
 *   2. Then emit live events; they merge into the assistant bubble.
 */
async function seedTurn(h: Awaited<ReturnType<typeof renderApp>>, sessionId: string) {
  const userMsg = makeUserMsg({ id: "u", content: "go", seq: 0 });
  const assistantMsg = makeAssistantMsg({
    id: "a",
    content: "",
    seq: 1,
    isStreaming: true,
  });
  h.emit({
    type: "messages_replay",
    data: { app_session_id: sessionId, messages: [userMsg, assistantMsg] },
  });
  await h.flush();
}

describe("live event accumulation onto canonical messages", () => {
  it("routes live primary events to the active run target, not the last assistant", async () => {
    // Team mode: live-event text renders through the entity-block
    // timeline (src/strategies/Strategy.ts buildEntityBlocks requires
    // team mode or a worker panel — native-mode prose streams via
    // message_content_updated instead, not the raw events array).
    const session = makeSession({ orchestration_mode: "team" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "messages_replay",
      data: {
        app_session_id: session.id,
        messages: [
          makeUserMsg({ id: "u1", content: "first", seq: 0 }),
          makeAssistantMsg({
            id: "active-assistant",
            content: "",
            seq: 1,
            isStreaming: true,
          }),
          makeUserMsg({ id: "u2", content: "second", seq: 2 }),
          makeAssistantMsg({
            id: "later-assistant",
            content: "old final text",
            seq: 3,
          }),
        ],
      },
    });
    h.emit({
      type: "run_state",
      data: {
        app_session_id: session.id,
        runs: [
          makeRun({
            kind: "manager",
            target_message_id: "active-assistant",
          }),
        ],
      },
    });
    await h.flush();

    h.emit({
      type: "agent_message",
      data: {
        app_session_id: session.id,
        uuid: "targeted-event",
        type: "assistant",
        message: {
          content: [{ type: "text", text: "new targeted chunk" }],
        },
      },
    });
    // Chat.tsx throttles turn-group re-renders to 1/140ms while the
    // session is running (useThrottledValue) — poll past that window
    // instead of a single flush.
    await h.waitFor(() =>
      (h.toJSON().chat.messages.find((m) => m.id === "active-assistant")?.text ?? "")
        .includes("new targeted chunk"),
    );

    const view = h.toJSON();
    expect(
      view.chat.messages.find((m) => m.id === "active-assistant")?.text ?? "",
    ).toContain("new targeted chunk");
    expect(
      view.chat.messages.find((m) => m.id === "later-assistant")?.text ?? "",
    ).not.toContain("new targeted chunk");
    h.unmount();
  });

  it("manager_event text accumulates inside the canonical assistant bubble", async () => {
    // Team mode: see the routing test above for why.
    const session = makeSession({ orchestration_mode: "team" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await seedTurn(h, session.id);

    h.emit({
      type: "manager_event",
      data: {
        app_session_id: session.id,
        event: {
          type: "agent_message",
          data: {
            type: "assistant",
            message: { content: [{ type: "text", text: "first chunk" }] },
          },
        },
      },
    });
    h.emit({
      type: "manager_event",
      data: {
        app_session_id: session.id,
        event: {
          type: "agent_message",
          data: {
            type: "assistant",
            message: { content: [{ type: "text", text: "second chunk" }] },
          },
        },
      },
    });
    // Chat.tsx throttles turn-group re-renders to 1/140ms while the
    // session is running (useThrottledValue) — poll past that window
    // instead of a single flush.
    await h.waitFor(() =>
      (h.toJSON().chat.messages.find((m) => m.id === "a")?.text ?? "").includes("second chunk"),
    );

    const assistant = h.toJSON().chat.messages.find((m) => m.id === "a");
    expect(assistant?.text ?? "").toContain("first chunk");
    expect(assistant?.text ?? "").toContain("second chunk");
    h.unmount();
  });

  it("worker_start adds a worker panel to the canonical assistant message", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await seedTurn(h, session.id);

    h.emit({
      type: "worker_start",
      data: {
        app_session_id: session.id,
        delegation_id: "d1",
        worker_session_id: "w-bc",
        worker_description: "Researcher",
        is_new: false,
        instructions_preview: "find X",
      },
    });
    await h.flush();

    const assistantDom = h.$('[data-testid="assistant-message"][data-message-id="a"]');
    expect(assistantDom?.textContent ?? "").toContain("Researcher");
    h.unmount();
  });

  it("turn_start updates manager_session_id WITHOUT clearing prior events", async () => {
    // Team mode: see the routing test above for why.
    const session = makeSession({ orchestration_mode: "team" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await seedTurn(h, session.id);

    h.emit({
      type: "manager_event",
      data: {
        app_session_id: session.id,
        event: {
          type: "agent_message",
          data: {
            type: "assistant",
            message: { content: [{ type: "text", text: "prelude" }] },
          },
        },
      },
    });
    // Chat.tsx throttles turn-group re-renders to 1/140ms while the
    // session is running (useThrottledValue) — poll past that window
    // instead of a single flush.
    await h.waitFor(() =>
      (h.toJSON().chat.messages.find((m) => m.id === "a")?.text ?? "").includes("prelude"),
    );
    expect(
      h.toJSON().chat.messages.find((m) => m.id === "a")?.text ?? "",
    ).toContain("prelude");

    // Real applyLiveTurnEvent semantics: turn_start preserves
    // existing events (idempotent re-broadcast on reconnect must not
    // wipe in-flight accumulated content). The wipe only happens when
    // the backend ships a fresh messages_replay that REPLACES the
    // assistant message wholesale.
    h.emit({
      type: "turn_start",
      data: {
        app_session_id: session.id,
        session_id: session.id,
        manager_session_id: "sid-1",
      },
    });
    await h.flush();

    expect(
      h.toJSON().chat.messages.find((m) => m.id === "a")?.text ?? "",
    ).toContain("prelude");
    h.unmount();
  });

  it("workers_changed event with cwd=null (global) refetches even when cwd differs", async () => {
    const session = makeSession({ cwd: "/proj/a", orchestration_mode: "team" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.flush();

    const before = h.restCalls.filter(
      (c) => c.method === "GET" && c.path === "/api/workers",
    ).length;

    h.emit({ type: "workers_changed", data: { cwd: null } });
    await h.flush();

    const after = h.restCalls.filter(
      (c) => c.method === "GET" && c.path === "/api/workers",
    ).length;
    expect(after).toBeGreaterThan(before);
    h.unmount();
  });

  it("workers_changed for a different cwd refetches global workers", async () => {
    const session = makeSession({ cwd: "/proj/a", orchestration_mode: "team" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.flush();

    const before = h.restCalls.filter(
      (c) => c.method === "GET" && c.path === "/api/workers",
    ).length;

    h.emit({ type: "workers_changed", data: { cwd: "/proj/b" } });
    await h.flush();

    const after = h.restCalls.filter(
      (c) => c.method === "GET" && c.path === "/api/workers",
    ).length;
    expect(after).toBeGreaterThan(before);
    h.unmount();
  });

  it("a second send while a run is active does not crash and queues the WS frame", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.typeAndSend("first");

    h.emit({
      type: "run_state",
      data: {
        app_session_id: session.id,
        runs: [makeRun({ target_message_id: null })],
      },
    });
    await h.flush();

    await h.typeAndSend("second");

    const sends = h.outbound.filter((f) => f.type === "send_message");
    expect(sends).toHaveLength(2);
    expect(sends[0]).toMatchObject({ prompt: "first" });
    expect(sends[1]).toMatchObject({ prompt: "second" });
    h.unmount();
  });
});
