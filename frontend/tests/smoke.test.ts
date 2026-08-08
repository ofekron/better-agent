import { describe, it, expect } from "vitest";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeOperatorMsg, makeSession, makeUserMsg } from "./fixtures";
import { promptNode, assistantTextNode } from "./surface/fixtures";

describe("harness smoke", () => {
  it("boots with a seeded session and lists it in the sidebar", async () => {
    const h = await renderApp({
      seed: { sessions: [makeSession()] },
    });

    // Auto-select redirects the Ask home to the only session instead of
    // sitting empty (src/autoSelectSession.ts), so it's active on boot.
    const view = h.toJSON();
    expect(view.sidebar.sessions).toEqual([
      { id: "sess-1", name: expect.stringContaining("test session"), active: true },
    ]);
    expect(view.input.disabled).toBe(false);

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
        makeOperatorMsg({
          id: "file-edit-ask",
          content: "Which file or files do you want to edit?",
          seq: 0,
          source: "file_editor",
        }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const prompt = h.raw.getByText(/Which file or files do you want to edit/);
    const pickFiles = h.$('[data-testid="empty-file-editor-pick-files"]');

    expect(pickFiles).not.toBeNull();
    expect(
      Boolean(prompt!.compareDocumentPosition(pickFiles!) & Node.DOCUMENT_POSITION_FOLLOWING),
    ).toBe(true);

    await h.click('[data-testid="empty-file-editor-pick-files"]');

    expect(h.raw.getByRole("heading", { name: "fileChooser.title" })).toBeTruthy();

    h.unmount();
  });

  it("hides the empty file picker after the user sends a prompt", async () => {
    const session = makeSession({
      id: "file-edit-empty-prompted",
      name: "Edit project files",
      working_mode: "file_editing",
      working_mode_meta: {
        persistent: true,
        project_cwd: "/tmp/proj",
        file_paths: [],
        original_contents: {},
      },
      messages: [
        makeOperatorMsg({
          id: "file-edit-ask",
          content: "Which file or files do you want to edit?",
          seq: 0,
          source: "file_editor",
        }),
        makeUserMsg({
          id: "file-edit-user",
          content: "create a new config file",
          seq: 1,
        }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    expect(h.$('[data-testid="empty-file-editor-pick-files"]')).toBeNull();

    h.unmount();
  });

  it("hides the empty file picker while the first prompt is pending", async () => {
    const session = makeSession({
      id: "file-edit-empty-pending",
      name: "Edit project files",
      working_mode: "file_editing",
      working_mode_meta: {
        persistent: true,
        project_cwd: "/tmp/proj",
        file_paths: [],
        original_contents: {},
      },
      messages: [
        makeOperatorMsg({
          id: "file-edit-ask",
          content: "Which file or files do you want to edit?",
          seq: 0,
          source: "file_editor",
        }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.typeAndSend("create a new config file");

    expect(h.$('[data-testid="empty-file-editor-pick-files"]')).toBeNull();

    h.unmount();
  });

  it("native surface: send confirms via typed_prompt + assistant_text node_upsert frames", async () => {
    // Native send (Phase I stage 2c): the composer routes through
    // SurfaceStore.sendPrompt over `/ws/v2/surface` — a provisional
    // "sending" typed_prompt appears IMMEDIATELY (ChatSurfaceView's own
    // optimistic echo, tests/surface/nativeSend.test.tsx covers this
    // mechanism in isolation); it is replaced in place, not appended
    // alongside, once the backend's confirmed node arrives carrying the
    // SAME intent_id `sendPrompt` generated (see fixtures.ts's `promptNode`
    // trailing `intentId` param).
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-send", messages: [] });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession("sess-send");
    await h.waitFor(() => h.$('[data-testid="surface-chat-view"]') !== null);

    await h.typeAndSend("hello");

    const sent = (h.outbound as { intent?: { kind?: string; intent_id?: string; text?: string; session_id?: string } }[])
      .find((f) => f.intent?.kind === "send_prompt");
    expect(sent).toMatchObject({ intent: { kind: "send_prompt", text: "hello", session_id: "sess-send" } });
    const intentId = sent!.intent!.intent_id!;

    // The optimistic provisional turn is already visible, pre-confirmation.
    expect(h.$('[data-testid="surface-turn"]')).not.toBeNull();
    expect(h.$('[data-testid="surface-prompt-status"]')?.className).toContain("status-sending");

    h.emitSurface("sess-send", {
      type: "node_upsert",
      cv: 1,
      surface_id: "sess-send",
      snapshot: { incarnation: "mock", render_rev: 1, hist_rev: 0 },
      node: promptNode("t1", "hello", "sess-send", intentId),
    });
    // A turn born entirely from live frames (never present at hydrate, so
    // never eager-seeded) is live, which makes TurnView's useChildren fire
    // an on-demand `ensureChildren` REST fetch for its body the moment it
    // mounts (surface/useChildren.ts) — delivered here back-to-back with no
    // wait, so that fetch's response is guaranteed to still be in flight
    // when this live node_upsert lands. `SurfaceStore.setChildrenTable`
    // merges a late-resolving fetch response by node identity/cv instead of
    // replacing the table wholesale, so the assistant text below must
    // survive regardless of which one resolves last (regression coverage
    // for the ensureChildren-vs-live-upsert clobber race).
    h.emitSurface("sess-send", {
      type: "node_upsert",
      cv: 2,
      surface_id: "sess-send",
      snapshot: { incarnation: "mock", render_rev: 2, hist_rev: 0 },
      node: assistantTextNode("t1", "a1", "hi there", "turn:t1"),
    });

    await h.waitFor(() => {
      const el = h.$('[data-testid="surface-assistant-text"]');
      return !!el && (el.textContent ?? "").includes("hi there");
    });

    // Reconciled in place — one turn, no leftover "sending" chrome.
    expect(h.$$('[data-testid="surface-turn"]').length).toBe(1);
    expect(h.$('[data-testid="surface-prompt-status"]')).toBeNull();
    expect(h.$('[data-testid="surface-typed-prompt"]')?.textContent).toContain("hello");
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
    // The backend publishes authoritative stopped monitoring state with the
    // terminal error so the registry and failed-message chrome converge.
    h.emit({
      type: "session_monitoring_changed",
      data: {
        session_id: session.id,
        monitoring_state: "stopped",
        cwd: session.cwd,
        node_id: session.node_id ?? "primary",
      },
    });
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

  it("error terminal drops a streaming assistant placeholder instead of leaving 'No output'", async () => {
    const session = makeSession({
      messages: [
        makeUserMsg({ id: "u1", content: "do work", seq: 0 }),
        // In-flight assistant placeholder (streaming, no content yet).
        makeAssistantMsg({ id: "live-1", isStreaming: true, content: "", events: [] }),
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    await h.flush();

    expect(
      h.toJSON().chat.messages.find((m) => m.role === "assistant"),
    ).toBeDefined();

    // Backend exception path: it marks the USER message errored
    // (mark_user_error) and REMOVES the persisted assistant message,
    // then emits `error` with NO client_id and NO messages_delta. The
    // frontend must mirror that: surface the failure on the prompt
    // bubble and drop the orphan placeholder (no phantom "No output").
    h.emit({
      type: "error",
      data: { app_session_id: session.id, error: "kaboom" },
    });
    await h.flush();

    const view = h.toJSON();
    // The failure is surfaced in-chat on the prompt bubble…
    expect(h.raw.container.textContent).toMatch(/kaboom/);
    const failed = view.chat.messages.find(
      (m) => m.role === "user" && m.status === "error",
    );
    expect(failed).toBeDefined();
    // …and no phantom "No output" assistant turn is left behind.
    expect(h.raw.container.textContent).not.toMatch(/No output/);
    expect(
      view.chat.messages.find((m) => m.role === "assistant"),
    ).toBeUndefined();

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

  // The legacy "native-mode session does not show the manager-scope chip"
  // case is DELETED, not ported — it asserted the ABSENCE of legacy
  // MessageBubble/TurnGroup markup (`.manager-scope`, `.role-label-
  // manager`). Under `ba.surface_native`, ChatSurfaceView replaces that
  // whole render tree; no session, in any orchestration_mode, ever
  // produces those classes there. There is no native manager-scope
  // concept to test the absence of (see grep across src/surface/ — zero
  // hits), so porting the assertion would be vacuously true by construction.

  it("workers_changed WS event triggers a refetch of /api/workers", async () => {
    const session = makeSession();
    const h = await renderApp({
      seed: { sessions: [session] },
    });
    await h.selectSession(session.id);
    await h.flush();
    // The workers panel (an extension module) only mounts on the Workers
    // sidebar tab; open it so [data-testid="workers-panel"] is in the DOM.
    await h.clickByText(/sidebar\.workersTab/);

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
