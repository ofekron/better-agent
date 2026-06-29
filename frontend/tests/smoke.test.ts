import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";

describe("harness smoke", () => {
  it("boots with a seeded session and lists it in the sidebar", async () => {
    const h = await renderApp({
      seed: { sessions: [makeSession()] },
    });

    const view = h.toJSON();
    expect(view.sidebar.sessions).toEqual([
      { id: "sess-1", name: expect.stringContaining("test session"), active: false },
    ]);
    expect(view.input.disabled).toBe(true);

    h.unmount();
  });

  it("shows a file picker action for empty file-edit sessions", async () => {
    const session = makeSession({
      id: "file-edit-empty",
      name: "Edit project files",
      working_mode: "file_editing",
      working_mode_meta: {
        persistent: true,
        project_cwd: "/tmp/proj",
        file_paths: [],
        original_contents: {},
      },
      messages: [
        makeAssistantMsg({
          id: "file-edit-ask",
          content: "Which file or files do you want to edit?",
          seq: 0,
        }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    expect(h.$('[data-testid="empty-file-editor-pick-files"]')).not.toBeNull();

    await h.click('[data-testid="empty-file-editor-pick-files"]');

    expect(h.raw.getByRole("heading", { name: "fileChooser.title" })).toBeTruthy();

    h.unmount();
  });

  it("send → messages_replay populates the chat (new architecture)", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    let view = h.toJSON();
    expect(view.chat.visible).toBe(true);
    expect(view.input.disabled).toBe(false);

    await h.typeAndSend("hello");

    expect(h.outbound.find((f) => f.type === "send_message")).toMatchObject({
      type: "send_message",
      prompt: "hello",
      app_session_id: session.id,
      orchestration_mode: "manager",
      client_id: expect.stringMatching(/^pending-/),
    });
    view = h.toJSON();
    expect(
      view.chat.messages.some((m) => m.role === "user" && m.status === "sending"),
    ).toBe(true);

    // Backend echoes the canonical user_message with client_id, then sends
    // messages_replay carrying the freshly-persisted assistant message.
    const sentFrame = h.outbound.find((f) => f.type === "send_message")!;
    const clientId = sentFrame.client_id as string;
    const userMsg = makeUserMsg({
      id: "u1",
      content: "hello",
      client_id: clientId,
      seq: 0,
    });
    const assistantMsg = makeAssistantMsg({
      id: "a1",
      content: "hi there",
      seq: 1,
    });

    h.emit({
      type: "user_message_persisted",
      data: { session_id: session.id, user_message: userMsg },
    });
    h.emit({
      type: "messages_replay",
      data: { app_session_id: session.id, messages: [userMsg, assistantMsg] },
    });
    h.emit({ type: "turn_complete", data: { session_id: session.id, success: true } });
    await h.flush();

    view = h.toJSON();
    expect(view.chat.messages.find((m) => m.id === "u1")?.role).toBe("user");
    const assistant = view.chat.messages.find((m) => m.id === "a1");
    expect(assistant?.role).toBe("assistant");
    expect(assistant?.text).toContain("hi there");
    // Optimistic pending was matched and removed by client_id.
    expect(
      view.chat.messages.some((m) => m.role === "user" && m.status === "sending"),
    ).toBe(false);
    h.unmount();
  });

  it("pending approval rehydrates from REST → approve sends REST", async () => {
    const session = makeSession();
    const h = await renderApp({
      seed: {
        sessions: [session],
        approvals: [
          {
            delegation_id: "deleg-1",
            app_session_id: session.id,
            cwd: session.cwd,
            justification: "need a researcher",
            proposed_description: "Researcher",
            proposed_orchestration_mode: "native",
            instructions_preview: "Find X",
            model: "claude-sonnet-4-6",
            status: "pending",
            created_at: new Date().toISOString(),
            expires_at: new Date(Date.now() + 86400_000).toISOString(),
          },
        ],
      },
    });
    await h.selectSession(session.id);
    await h.flush();

    const view = h.toJSON();
    expect(view.chat.approvals).toHaveLength(1);
    expect(view.chat.approvals[0]).toMatchObject({ delegationId: "deleg-1" });
    expect(view.chat.approvals[0].text).toContain("need a researcher");

    await h.approveWorker("deleg-1");
    expect(
      h.restCalls.find(
        (c) => c.path === "/api/pending_approvals/deleg-1/approve" && c.method === "POST",
      ),
    ).toBeDefined();

    h.unmount();
  });

  it("error event marks the pending message failed; nothing crashes", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.typeAndSend("oops");

    h.emit({ type: "turn_start", data: { session_id: session.id } });
    h.emit({ type: "error", data: { error: "boom", session_id: session.id } });
    await h.flush();

    const view = h.toJSON();
    // The optimistic user bubble flips to status="error"; the assistant
    // never gets persisted (since the error fires before user_message_persisted).
    const failed = view.chat.messages.find(
      (m) => m.role === "user" && m.status === "error",
    );
    expect(failed).toBeDefined();
    // No active runs — error event drives the streaming flag back to false.
    expect(view.chat.running).toBe(false);
    expect(view.input.disabled).toBe(false);

    h.unmount();
  });

  it("correlated send error keeps the failed prompt visible and clears retry backlog", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.typeAndSend("bad payload");

    const sentFrame = h.outbound.find((f) => f.type === "send_message")!;
    const clientId = sentFrame.client_id as string;
    expect(localStorage.getItem("better_agent_offline_queue")).not.toBeNull();

    h.emit({
      type: "error",
      data: {
        app_session_id: session.id,
        session_id: session.id,
        client_id: clientId,
        error: "capability_contexts must be a list",
      },
    });
    await h.flush();

    const failed = h.toJSON().chat.messages.find((m) => m.id === clientId);
    expect(failed?.status).toBe("error");
    expect(localStorage.getItem("better_agent_offline_queue")).toBeNull();

    h.unmount();
  });

  it("native-mode session does not show the manager-scope chip", async () => {
    const session = makeSession({ id: "sess-native", orchestration_mode: "native" });
    const userMsg = makeUserMsg({ id: "un", content: "hi" });
    // Native mode: assistantMessage carries no `manager` field.
    const assistantMsg = makeAssistantMsg({
      id: "an",
      content: "ok",
      manager: undefined,
    });
    session.messages = [userMsg, assistantMsg];
    session.native_claude_session_id = "claude-sid-1";
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.flush();

    const view = h.toJSON();
    expect(view.chat.messages.map((m) => m.id)).toEqual(["un", "an"]);
    // Manager-scope wrapper renders a "Manager" chip; native mode must not.
    const container = h.raw.container as HTMLElement;
    expect(container.querySelector(".manager-scope")).toBeNull();
    expect(container.querySelector(".role-label-manager")).toBeNull();
    // The workers panel only renders in manager mode + cwd present.
    expect(view.sidebar.workersPanelVisible).toBe(false);

    h.unmount();
  });

  it("workers_changed WS event triggers a refetch of /api/workers", async () => {
    const session = makeSession();
    const h = await renderApp({
      seed: { sessions: [session] },
    });
    await h.selectSession(session.id);
    await h.flush();

    const callsBefore = h.restCalls.filter(
      (c) => c.method === "GET" && c.path === "/api/workers",
    ).length;
    expect(callsBefore).toBeGreaterThan(0);

    // Simulate the backend mutating worker registry, then pushing.
    h.backend.state.workers = [
      {
        agent_session_id: "w1",
        name: "Indexer",
        orchestration_mode: "native",
        initialized: true,
        delegation_count: 3,
      },
    ];
    h.emit({ type: "workers_changed", data: { cwd: session.cwd } });
    await h.flush();

    const callsAfter = h.restCalls.filter(
      (c) => c.method === "GET" && c.path === "/api/workers",
    ).length;
    expect(callsAfter).toBeGreaterThan(callsBefore);
    expect(h.toJSON().sidebar.workerCount).toBe(1);

    h.unmount();
  });

  it("session_renamed WS event updates the sidebar name without a refetch", async () => {
    const session = makeSession({ name: "old name" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.flush();

    h.emit({
      type: "session_renamed",
      data: { session_id: session.id, name: "renamed by backend" },
    });
    await h.flush();

    const view = h.toJSON();
    expect(view.sidebar.sessions[0].name).toContain("renamed by backend");
    // The rename came via WS, no /api/sessions/:id/rename PUT should have fired.
    expect(
      h.restCalls.find(
        (c) => c.method === "PUT" && c.path === `/api/sessions/${session.id}/rename`,
      ),
    ).toBeUndefined();

    h.unmount();
  });
});
