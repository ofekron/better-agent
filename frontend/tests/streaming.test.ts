import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeRun, makeSession, makeUserMsg } from "./fixtures";

describe("streaming + multi-session behavior", () => {
  it("Stop button sends a stop_message frame for the current session", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.typeAndSend("go");

    // Backend pushes a run_state with one active run → run badge + Stop
    // button render under the optimistic user bubble (target null = unanchored).
    h.emit({
      type: "run_state",
      data: {
        app_session_id: session.id,
        runs: [makeRun({ kind: "manager", target_message_id: null })],
      },
    });
    await h.flush();

    expect(h.toJSON().chat.stopButtonVisible).toBe(true);
    await h.clickStop();

    expect(h.outbound).toContainEqual(
      expect.objectContaining({ type: "stop_message", app_session_id: session.id }),
    );
    h.unmount();
  });

  it("Stop button falls back to REST with progress when websocket is closed", async () => {
    const session = makeSession({ is_running: true, monitoring_state: "active" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    h.dropConnection();
    const releaseStop = h.backend.holdNext("POST", `/api/sessions/${session.id}/stop`);

    expect(h.toJSON().chat.stopButtonVisible).toBe(true);
    await h.clickStop();

    expect(h.outbound).not.toContainEqual(
      expect.objectContaining({ type: "stop_message", app_session_id: session.id }),
    );
    expect(h.restCalls).toContainEqual(
      expect.objectContaining({
        method: "POST",
        path: `/api/sessions/${session.id}/stop`,
      }),
    );
    expect(h.toJSON().chat.stopButtonDisabled).toBe(true);
    expect(h.toJSON().chat.stopButtonStopping).toBe(true);

    releaseStop();
    await h.flush();
    expect(h.toJSON().chat.stopButtonDisabled).toBe(false);
    expect(h.toJSON().chat.stopButtonStopping).toBe(false);
    h.unmount();
  });

  it("empty run_state event clears the run badges and the Stop button", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.typeAndSend("go");

    h.emit({
      type: "run_state",
      data: {
        app_session_id: session.id,
        runs: [makeRun({ target_message_id: null })],
      },
    });
    await h.flush();
    expect(h.toJSON().chat.running).toBe(true);

    // Backend reports nothing running.
    h.emit({
      type: "run_state",
      data: { app_session_id: session.id, runs: [] },
    });
    await h.flush();

    const view = h.toJSON();
    expect(view.chat.running).toBe(false);
    expect(view.chat.stopButtonVisible).toBe(false);
    h.unmount();
  });

  it("runs from session A do not appear when viewing session B", async () => {
    const a = makeSession({ id: "a" });
    const b = makeSession({ id: "b", name: "B" });
    const h = await renderApp({ seed: { sessions: [a, b] } });
    await h.selectSession("a");
    await h.typeAndSend("on A");

    h.emit({
      type: "run_state",
      data: {
        app_session_id: "a",
        runs: [makeRun({ target_message_id: null })],
      },
    });
    await h.flush();
    expect(h.toJSON().chat.running).toBe(true);

    // Switch to B — A's runs don't bleed in.
    await h.selectSession("b");
    expect(h.toJSON().chat.running).toBe(false);
    h.unmount();
  });

  it("Send button is disabled when textarea is empty", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const view = h.toJSON();
    expect(view.input.text).toBe("");
    expect(view.input.sendDisabled).toBe(true);
    h.unmount();
  });

  it("Empty send_message frame is never produced (button gated)", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    // Click Send without typing — should be a no-op.
    const btn = h.$('[data-testid="send-btn"]') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(h.outbound.find((f) => f.type === "send_message")).toBeUndefined();
    h.unmount();
  });

  it("textarea clears after a successful send and re-enables", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.typeAndSend("hello world");

    const view = h.toJSON();
    expect(view.input.text).toBe("");
    // Still enabled while streaming; it's only disabled when no session.
    expect(view.input.disabled).toBe(false);
    h.unmount();
  });

  it("orchestration_mode in send_message frame matches the session", async () => {
    const session = makeSession({ orchestration_mode: "native" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.typeAndSend("hi");

    const send = h.outbound.find((f) => f.type === "send_message");
    expect(send).toMatchObject({ orchestration_mode: "native" });
    h.unmount();
  });

  it("a manager-mode persisted assistant renders the Manager scope chip", async () => {
    const userMsg = makeUserMsg({ id: "u", content: "hi" });
    const assistantMsg = makeAssistantMsg({
      id: "a",
      content: "ok",
      manager: { session_id: "claude-sid-1", events: [] },
    });
    const session = makeSession({
      orchestration_mode: "manager",
      messages: [userMsg, assistantMsg],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    expect(h.$(".manager-scope")).not.toBeNull();
    expect(h.$(".role-label-manager")).not.toBeNull();
    h.unmount();
  });
});
