import { describe, expect, it } from "vitest";
import { fireEvent } from "@testing-library/react";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeRun, makeSession, makeUserMsg } from "./fixtures";
import { buildInlineTagsPreamble } from "../src/utils/inlineTagsPrompt";
import type { InlineTag } from "../src/types/inlineTag";
import { node, promptNode, turnNode, compactTurn } from "./surface/fixtures";

function setViewportWidth(width: number) {
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    writable: true,
    value: width,
  });
  window.dispatchEvent(new Event("resize"));
}

async function typeAndSteer(h: Awaited<ReturnType<typeof renderApp>>, text: string) {
  const input = h.$('[data-testid="input-textarea"]') as HTMLTextAreaElement | null;
  if (!input) throw new Error("input textarea not present");
  fireEvent.change(input, { target: { value: text } });
  for (let i = 0; i < 30 && !h.$('[data-testid="send-btn"]'); i++) {
    await h.flush();
  }
  await h.click('[data-testid="send-btn"]');
}

async function typeAndQueue(h: Awaited<ReturnType<typeof renderApp>>, text: string) {
  const input = h.$('[data-testid="input-textarea"]') as HTMLTextAreaElement | null;
  if (!input) throw new Error("input textarea not present");
  fireEvent.change(input, { target: { value: text } });
  for (
    let i = 0;
    i < 30 && !h.$('[data-testid="queue-btn"]') && h.$('[data-testid="send-btn"]')?.textContent !== "Queue";
    i++
  ) {
    await h.flush();
  }
  if (h.$('[data-testid="queue-btn"]')) {
    await h.click('[data-testid="queue-btn"]');
    return;
  }
  if (h.$('[data-testid="send-btn"]')?.textContent === "Queue") {
    await h.click('[data-testid="send-btn"]');
    return;
  }
  throw new Error("queue action not present");
}

async function pressEmptyEnter(h: Awaited<ReturnType<typeof renderApp>>) {
  const input = h.$('[data-testid="input-textarea"]') as HTMLTextAreaElement | null;
  if (!input) throw new Error("input textarea not present");
  fireEvent.keyDown(input, { key: "Enter" });
  await h.flush();
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
  it("appends multiple queued drafts without replacing the first queued banner", async () => {
    const session = makeSession({
      provider_id: "codex",
      messages: [
        makeUserMsg({ id: "u1", content: "start work" }),
        makeAssistantMsg({ id: "a1", isStreaming: true }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    h.setMonitoring(session.id, "active");
    h.emit({
      type: "run_state",
      data: { app_session_id: session.id, runs: [makeRun({ target_message_id: "a1" })] },
    });
    await h.flush();

    await typeAndQueue(h, "first queued");
    h.emit({
      type: "prompt_queued",
      data: {
        app_session_id: session.id,
        queued_id: "q1",
        prompt_preview: "first queued",
        send_mode: "queue",
        queue_position: 1,
        client_id: h.outbound.find((frame) => frame.type === "send_message")?.client_id,
      },
    });
    await h.flush();

    await typeAndQueue(h, "second queued");
    const sends = h.outbound.filter((frame) => frame.type === "send_message");
    h.emit({
      type: "prompt_queued",
      data: {
        app_session_id: session.id,
        queued_id: "q2",
        prompt_preview: "second queued",
        send_mode: "queue",
        queue_position: 2,
        client_id: sends[1]?.client_id,
      },
    });
    await h.flush();

    expect(sends).toHaveLength(2);
    expect(sends.map((frame) => frame.prompt)).toEqual(["first queued", "second queued"]);
    expect(sends.every((frame) => frame.send_mode === "queue")).toBe(true);
    expect(h.outbound.some((frame) => frame.type === "cancel_queued")).toBe(false);
    const banners = h.$$('[data-testid="queued-prompt-banner"]');
    expect(banners).toHaveLength(2);
    expect(banners[0]?.textContent).toContain("first queued");
    expect(banners[1]?.textContent).toContain("second queued");
    h.unmount();
  });

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
        kind: "queued_behind",
      }],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    h.setMonitoring(session.id, "active");
    h.emit({
      type: "run_state",
      data: { app_session_id: session.id, runs: [makeRun({ target_message_id: "a1" })] },
    });
    await h.flush();

    expect(h.$('[data-testid="queued-prompt-banner"]')?.textContent).toContain("queued steer");
    await typeAndQueue(h, "draft steer");

    expect(await waitForOutboundSend(h, "draft steer")).toMatchObject({
      type: "send_message",
      app_session_id: session.id,
      prompt: "draft steer",
      send_mode: "queue",
    });
    expect(h.outbound.some((frame) => frame.type === "cancel_queued")).toBe(false);
    expect(h.$('[data-testid="queued-prompt-banner"]')?.textContent).toContain("queued steer");
    h.unmount();
  });

  it("steers the queued prompt on empty Enter for steerable streaming sessions", async () => {
    setViewportWidth(1280);
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
        kind: "queued_behind",
      }],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    h.setMonitoring(session.id, "active");
    h.emit({
      type: "run_state",
      data: { app_session_id: session.id, runs: [makeRun({ target_message_id: "a1" })] },
    });
    await h.flush();

    expect(h.$('[data-testid="queued-prompt-banner"]')?.textContent).toContain("queued steer");
    await pressEmptyEnter(h);

    expect(h.outbound).toContainEqual(
      expect.objectContaining({
        type: "promote_queued",
        app_session_id: session.id,
        action: "steer",
      }),
    );
    expect(h.outbound).not.toContainEqual(
      expect.objectContaining({
        type: "promote_queued",
        app_session_id: session.id,
        action: "interrupt",
      }),
    );
    expect(h.$('[data-testid="queued-prompt-banner"]')).toBeNull();
    h.unmount();
  });

  it("does not reuse a steered prompt when the next queued send beats metadata", async () => {
    setViewportWidth(1280);
    const tag: InlineTag = {
      id: "tag-after-steer",
      messageId: "u1",
      selectedText: "selected code",
      comment: "apply to the next prompt",
      timestamp: "2026-07-18T12:00:00.000Z",
    };
    const session = makeSession({
      provider_id: "codex",
      messages: [
        makeUserMsg({ id: "u1", content: "start work" }),
        makeAssistantMsg({ id: "a1", isStreaming: true }),
      ],
      inline_tags: [tag],
      queued_prompts: [{
        id: "q1",
        content: "already steered",
        client_id: "client-q1",
        kind: "queued_behind",
      }],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    h.setMonitoring(session.id, "active");
    h.emit({
      type: "run_state",
      data: { app_session_id: session.id, runs: [makeRun({ target_message_id: "a1" })] },
    });
    await h.flush();

    await h.click('[data-testid="queued-steer-btn"]');
    expect(h.$('[data-testid="queued-prompt-banner"]')).toBeNull();

    await typeAndQueue(h, "next queued work");
    const sent = h.outbound.filter((frame) => frame.type === "send_message").at(-1);
    expect(sent).toMatchObject({
      prompt: expect.stringContaining("next queued work"),
      send_mode: "queue",
    });
    expect(String(sent?.prompt ?? "")).not.toContain("already steered");

    h.emit({
      type: "prompt_queued",
      data: {
        app_session_id: session.id,
        queued_id: "q2",
        prompt_preview: String(sent?.prompt ?? ""),
        send_mode: "queue",
        queue_position: 1,
        client_id: sent?.client_id,
      },
    });
    await h.flush();

    const banners = h.$$('[data-testid="queued-prompt-banner"]');
    expect(banners).toHaveLength(1);
    expect(banners[0]?.textContent).toContain("next queued work");
    expect(banners[0]?.textContent).not.toContain("already steered");
    h.unmount();
  });

  it("clears a steered queue item for a non-initiating subscriber", async () => {
    const tag: InlineTag = {
      id: "tag-after-remote-steer",
      messageId: "u1",
      selectedText: "selected code",
      comment: "apply to the next prompt",
      timestamp: "2026-07-18T12:00:00.000Z",
    };
    const session = makeSession({
      provider_id: "codex",
      messages: [
        makeUserMsg({ id: "u1", content: "start work" }),
        makeAssistantMsg({ id: "a1", isStreaming: true }),
      ],
      inline_tags: [tag],
      queued_prompts: [],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    h.setMonitoring(session.id, "active");
    h.emit({
      type: "run_state",
      data: { app_session_id: session.id, runs: [makeRun({ target_message_id: "a1" })] },
    });
    h.emit({
      type: "prompt_queued",
      data: {
        app_session_id: session.id,
        queued_id: "q1",
        prompt_preview: "steered in another tab",
        send_mode: "queue",
        queue_position: 1,
        client_id: "client-q1",
      },
    });
    await h.flush();

    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: session.id,
        patch: { queued_prompts: [] },
      },
    });
    await h.flush();
    expect(h.$('[data-testid="queued-prompt-banner"]')?.textContent)
      .toContain("steered in another tab");

    h.emit({
      type: "queue_consumed",
      data: { app_session_id: session.id, queued_id: "q1" },
    });
    await h.flush();
    expect(h.$('[data-testid="queued-prompt-banner"]')).toBeNull();

    await typeAndQueue(h, "next queued work");
    const sent = h.outbound.filter((frame) => frame.type === "send_message").at(-1);
    expect(sent).toMatchObject({
      prompt: expect.stringContaining("next queued work"),
      send_mode: "queue",
    });
    expect(String(sent?.prompt ?? "")).not.toContain("steered in another tab");
    h.unmount();
  });

  it("keeps later persisted prompts when the first queue item is consumed", async () => {
    const session = makeSession({
      queued_prompts: [
        { id: "q1", content: "first", client_id: "client-q1", kind: "queued_behind" },
        { id: "q2", content: "second", client_id: "client-q2", kind: "queued_behind" },
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "queue_consumed",
      data: { app_session_id: session.id, queued_id: "q1" },
    });
    await h.flush();

    const banners = h.$$('[data-testid="queued-prompt-banner"]');
    expect(banners).toHaveLength(1);
    expect(banners[0]?.textContent).toContain("second");
    h.unmount();
  });

  it("keeps later persisted prompts when the first user message is acknowledged", async () => {
    const session = makeSession({
      queued_prompts: [
        { id: "q1", content: "first", client_id: "client-q1", kind: "queued_behind" },
        { id: "q2", content: "second", client_id: "client-q2", kind: "queued_behind" },
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    h.emit({
      type: "user_message_persisted",
      data: {
        session_id: session.id,
        user_message: makeUserMsg({
          id: "persisted-q1",
          content: "first",
          client_id: "client-q1",
        }),
      },
    });
    await h.flush();

    const banners = h.$$('[data-testid="queued-prompt-banner"]');
    expect(banners).toHaveLength(1);
    expect(banners[0]?.textContent).toContain("second");
    h.unmount();
  });

  it("native surface: steer prompt renders inside the running turn, not as a new turn", async () => {
    // Native equivalent of the legacy "steer renders inside the assistant
    // turn, optimistic user bubble removed" case. Native has no separate
    // optimistic user bubble to remove (ChatSurfaceView never locally
    // projects a just-sent prompt — see smoke.test.ts's send test) so the
    // portable intent is narrower: a steer confirmed by the backend
    // arrives as a `steering_message` node parented under the SAME turn
    // that is running, and never spawns a second `surface-turn`/
    // `surface-typed-prompt`.
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-steer", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-steer", {
          turns: [compactTurn(turnNode("t1"), promptNode("t1", "start work"), [])],
        });
      },
    });
    await h.selectSession("sess-steer");
    await h.waitFor(() => h.$$('[data-testid="surface-turn"]').length === 1);

    h.emitSurface("sess-steer", {
      type: "turn_lifecycle",
      cv: 1,
      surface_id: "sess-steer",
      snapshot: { incarnation: "mock", render_rev: 1, hist_rev: 0 },
      turn_id: "t1",
      phase: "running",
      reason: null,
      usage: null,
    });
    // The turn just went live, which makes TurnView's useChildren fire an
    // on-demand `ensureChildren` REST fetch for its (still-empty) body —
    // delivered here back-to-back with no wait, so that fetch is guaranteed
    // to still be in flight when this live steering_message upsert lands
    // (regression coverage for the ensureChildren-vs-live-upsert clobber
    // race; see smoke.test.ts's send test for the fuller writeup, and
    // SurfaceStore.setChildrenTable for the fix).
    h.emitSurface("sess-steer", {
      type: "node_upsert",
      cv: 2,
      surface_id: "sess-steer",
      snapshot: { incarnation: "mock", render_rev: 2, hist_rev: 0 },
      node: node({
        node_id: "t1:steer-1",
        turn_id: "t1",
        kind: "steering_message",
        parent_id: "turn:t1",
        payload: { text: "steer inside turn", target: "a1", attachments: [] },
      }),
    });

    await h.waitFor(() => h.$('[data-testid="surface-steering-message"]') !== null);
    expect(h.$('[data-testid="surface-steering-message"]')?.textContent).toContain(
      "steer inside turn",
    );
    // The steer did not spawn a separate turn or prompt.
    expect(h.$$('[data-testid="surface-turn"]')).toHaveLength(1);
    expect(h.$$('[data-testid="surface-typed-prompt"]')).toHaveLength(1);
    h.unmount();
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

  it("treats authoritative monitoring as active before run detail arrives", async () => {
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
    h.emit({
      type: "session_monitoring_changed",
      data: {
        session_id: session.id,
        monitoring_state: "active",
        cwd: session.cwd,
        node_id: session.node_id ?? "primary",
      },
    });
    await h.flush();
    await typeAndSteer(h, "steer before run_state");

    const sent = await waitForOutboundSend(h, "steer before run_state");
    expect(sent).toMatchObject({
      send_mode: "steer",
    });
    h.unmount();
  });
});
