import { describe, it, expect } from "vitest";
import { fireEvent } from "@testing-library/react";
import { renderApp } from "./harness";
import { makeSession, makeUserMsg } from "./fixtures";
import { loadOfflineActions } from "src/lib/offlineQueueStore";

async function waitForSend(
  h: Awaited<ReturnType<typeof renderApp>>,
  prompt: string,
) {
  for (let i = 0; i < 10; i++) {
    const sent = h.outbound.find(
      (frame) => frame.type === "send_message" && frame.prompt === prompt,
    );
    if (sent) return sent;
    await h.flush();
  }
  return undefined;
}

async function waitForSelector(
  h: Awaited<ReturnType<typeof renderApp>>,
  selector: string,
) {
  for (let i = 0; i < 10; i++) {
    const el = h.$(selector);
    if (el) return el;
    await h.flush();
  }
  return null;
}

async function clickNewSession(h: Awaited<ReturnType<typeof renderApp>>) {
  await h.clickByText(/^(\+ New|session\.newButton)$/);
}

describe("sessions CRUD + subscribe lifecycle", () => {
  it("changes a newly created empty session model before the first prompt", async () => {
    const h = await renderApp({
      seed: {
        sessions: [],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
        models: [
          { id: "claude-sonnet-4-6", name: "Sonnet 4.6" },
          { id: "claude-opus-4-7", name: "Opus 4.7" },
        ],
      },
    });
    await clickNewSession(h);
    await h.click(".modal-footer .btn-primary");
    await h.flush();

    await h.click(".input-overflow-trigger");
    await h.click(".session-selector-picker-button");
    const selects = h.$$(".session-model-picker-field select") as HTMLSelectElement[];
    const modelSelect = selects[1];
    expect(modelSelect).toBeDefined();
    fireEvent.change(modelSelect, { target: { value: "claude-opus-4-7" } });
    await h.clickByText("OK");

    expect(h.restCalls).toContainEqual(
      expect.objectContaining({
        method: "PATCH",
        path: "/api/sessions/sess-1/selectors",
        body: expect.objectContaining({ model: "claude-opus-4-7" }),
      }),
    );

    await h.typeAndSend("first prompt after model change");
    expect(await waitForSend(h, "first prompt after model change")).toEqual(
      expect.objectContaining({ model: "claude-opus-4-7" }),
    );
    h.unmount();
  });

  it("clicking '+ New' creates a session via REST and selects it", async () => {
    const h = await renderApp({
      seed: {
        sessions: [],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    await clickNewSession(h);
    await h.click(".modal-footer .btn-primary");

    const post = h.restCalls.find(
      (c) => c.method === "POST" && c.path === "/api/sessions",
    );
    expect(post).toBeDefined();
    const view = h.toJSON();
    expect(view.sidebar.sessions).toHaveLength(1);
    expect(view.sidebar.sessions[0].active).toBe(true);
    h.unmount();
  });

  it("selects a new session through route sync instead of createSession side effects", async () => {
    const h = await renderApp({
      seed: {
        sessions: [],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    await clickNewSession(h);
    await h.click(".modal-footer .btn-primary");

    const detailGetIndex = h.restCalls.findIndex(
      (c) => c.method === "GET" && c.path === "/api/sessions/sess-1",
    );
    const subscribeIndex = h.outbound.findIndex(
      (frame) =>
        frame.type === "subscribe" &&
        frame.app_session_id === "sess-1",
    );

    expect(window.location.pathname).toBe("/s/sess-1");
    expect(detailGetIndex).toBeGreaterThan(-1);
    expect(subscribeIndex).toBeGreaterThan(-1);
    expect(h.toJSON().sidebar.sessions[0].active).toBe(true);
    h.unmount();
  });

  it("keeps a newly created session open when active filters exclude it from the sidebar", async () => {
    const existing = makeSession({
      id: "existing",
      name: "Existing match",
      cwd: "/tmp/project",
    });
    const h = await renderApp({
      seed: {
        sessions: [existing],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });

    const search = h.$(".session-search input") as HTMLInputElement;
    expect(search).not.toBeNull();
    fireEvent.change(search, { target: { value: "Existing" } });
    await h.flush();

    await clickNewSession(h);
    await h.click(".modal-footer .btn-primary");
    await h.flush();

    expect(window.location.pathname).toBe("/s/sess-2");
    expect(h.restCalls).toContainEqual(
      expect.objectContaining({ method: "GET", path: "/api/sessions/sess-2" }),
    );
    expect(h.toJSON().chat.title).toContain("New Session");
    expect(h.toJSON().sidebar.sessions.map((session) => session.id)).toEqual(["existing"]);
    h.unmount();
  });

  it("applies an auto-title rename to a freshly created open session", async () => {
    const h = await renderApp({
      seed: {
        sessions: [],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    await clickNewSession(h);
    await h.click(".modal-footer .btn-primary");
    await h.flush();

    h.emit({
      type: "session_renamed",
      data: { session_id: "sess-1", name: "AI titled session" },
    });
    await h.flush();

    expect(h.toJSON().sidebar.sessions[0].name).toContain("AI titled session");
    expect(h.toJSON().chat.title).toContain("AI titled session");
    h.unmount();
  });

  it("creates a session through REST when WebSocket is disconnected but HTTP is online", async () => {
    const h = await renderApp({
      seed: {
        sessions: [],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    h.dropConnection();
    await h.flush();

    await clickNewSession(h);
    await h.click(".modal-footer .btn-primary");

    expect(
      h.restCalls.filter((c) => c.method === "POST" && c.path === "/api/sessions"),
    ).toHaveLength(1);
    expect(h.toJSON().sidebar.sessions).toHaveLength(1);
    await expect.poll(async () => (await loadOfflineActions()).length).toBe(0);
    h.unmount();
  });

  it("queues a new session with its prompt offline, then creates and sends it on reconnect", async () => {
    const h = await renderApp({
      seed: {
        sessions: [],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    h.dropConnection();
    h.backend.setOffline(true);
    await h.flush();
    await clickNewSession(h);

    const prompt = h.$(".ns-investigation-textarea") as HTMLTextAreaElement;
    expect(prompt).not.toBeNull();
    Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      "value",
    )!.set!.call(prompt, "remember to implement offline sessions");
    prompt.dispatchEvent(new Event("input", { bubbles: true }));
    await h.flush();
    await h.click(".modal-footer .btn-primary");

    expect(
      h.restCalls.filter((c) => c.method === "POST" && c.path === "/api/sessions"),
    ).toHaveLength(1);
    expect(h.toJSON().sidebar.sessions).toHaveLength(1);
    expect(h.toJSON().chat.messages).toContainEqual(
      expect.objectContaining({ status: "offline" }),
    );
    expect(h.toJSON().chat.messages[0].text).toContain(
      "remember to implement offline sessions",
    );
    await expect.poll(async () => (await loadOfflineActions()).length).toBe(1);

    h.backend.setOffline(false);
    h.reopenConnection();
    await h.flush();

    expect(
      h.restCalls.filter((c) => c.method === "POST" && c.path === "/api/sessions"),
    ).toHaveLength(2);
    expect(h.outbound).toContainEqual(
      expect.objectContaining({
        type: "send_message",
        prompt: "remember to implement offline sessions",
      }),
    );
    await expect.poll(async () => (await loadOfflineActions()).length).toBe(1);

    const sent = h.outbound.find(
      (frame) =>
        frame.type === "send_message" &&
        frame.prompt === "remember to implement offline sessions",
    );
    h.emit({
      type: "user_message_persisted",
      data: {
        session_id: sent!.app_session_id,
        user_message: makeUserMsg({
          content: "remember to implement offline sessions",
          client_id: sent!.client_id as string,
        }),
      },
    });
    await h.flush();

    await expect.poll(async () => (await loadOfflineActions()).length).toBe(0);
    h.unmount();
  });

  it("pressing Enter in the new-session initial prompt creates and sends", async () => {
    const h = await renderApp({
      seed: {
        sessions: [],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    await clickNewSession(h);

    const prompt = h.$(".ns-investigation-textarea") as HTMLTextAreaElement;
    Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      "value",
    )!.set!.call(prompt, "create and send from enter");
    prompt.dispatchEvent(new Event("input", { bubbles: true }));
    prompt.dispatchEvent(new KeyboardEvent("keydown", {
      key: "Enter",
      bubbles: true,
      cancelable: true,
    }));
    await h.flush();

    expect(
      h.restCalls.filter((c) => c.method === "POST" && c.path === "/api/sessions"),
    ).toHaveLength(1);
    expect(
      h.restCalls.find((c) => c.method === "POST" && c.path === "/api/sessions"),
    ).toEqual(expect.objectContaining({
      credentials: "include",
      body: expect.objectContaining({ cwd: "/tmp/project" }),
    }));
    expect(await waitForSend(h, "create and send from enter")).toEqual(
      expect.objectContaining({ type: "send_message" }),
    );
    h.unmount();
  });

  it("sends new-session initial prompt attachments with the first message", async () => {
    const h = await renderApp({
      seed: {
        sessions: [],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    await clickNewSession(h);

    const prompt = h.$(".ns-investigation-textarea") as HTMLTextAreaElement;
    Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      "value",
    )!.set!.call(prompt, "read this attachment");
    prompt.dispatchEvent(new Event("input", { bubbles: true }));

    const input = h.$('[data-testid="new-session-attachment-input"]') as HTMLInputElement;
    const file = new File(["hello"], "note.txt", { type: "text/plain" });
    Object.defineProperty(input, "files", { value: [file], configurable: true });
    input.dispatchEvent(new Event("change", { bubbles: true }));
    expect(await waitForSelector(h, ".file-preview-item")).not.toBeNull();

    await h.click(".modal-footer .btn-primary");
    await h.flush();

    expect(await waitForSend(h, "read this attachment")).toEqual(
      expect.objectContaining({
        type: "send_message",
        prompt: "read this attachment",
        files: [expect.objectContaining({
          name: "note.txt",
          data: "aGVsbG8=",
          media_type: "text/plain",
        })],
      }),
    );
    h.unmount();
  });

  it("opens a prefilled new-session modal from the composer with attachments", async () => {
    // Project path must match the session's cwd — once the project index
    // loads, belongsToProjectPath attributes sidebar rows by registered
    // project membership, not raw string equality; an unregistered cwd
    // would filter the session out of its own sidebar.
    const session = makeSession({ id: "source", messages: [] });
    const h = await renderApp({
      seed: {
        sessions: [session],
        projects: [{
          path: session.cwd,
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    await h.selectSession(session.id);

    const prompt = h.$('[data-testid="input-textarea"]') as HTMLTextAreaElement;
    Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      "value",
    )!.set!.call(prompt, "send elsewhere");
    prompt.dispatchEvent(new Event("input", { bubbles: true }));

    const attach = h.$('.input-row input[type="file"]') as HTMLInputElement;
    const file = new File(["payload"], "payload.txt", { type: "text/plain" });
    Object.defineProperty(attach, "files", { value: [file], configurable: true });
    attach.dispatchEvent(new Event("change", { bubbles: true }));
    expect(await waitForSelector(h, ".file-preview-item")).not.toBeNull();

    await h.click(".input-overflow-trigger");
    await h.click('[data-testid="send-to-new-session-btn"]');
    await h.flush();

    const modalPrompt = h.$(".ns-investigation-textarea") as HTMLTextAreaElement;
    expect(modalPrompt.value).toBe("send elsewhere");
    expect(h.$(".modal-content .file-preview-item .file-preview-name")?.textContent).toBe("payload.txt");

    await h.click(".modal-footer .btn-primary");
    await h.flush();

    expect(await waitForSend(h, "send elsewhere")).toEqual(
      expect.objectContaining({
        type: "send_message",
        prompt: "send elsewhere",
        app_session_id: "sess-2",
        files: [expect.objectContaining({
          name: "payload.txt",
          data: "cGF5bG9hZA==",
          media_type: "text/plain",
        })],
      }),
    );
    expect(h.toJSON().input.text).toBe("");
    h.unmount();
  });

  it("opens a prefilled new-session modal from the composer prompt", async () => {
    const session = makeSession({ id: "source", messages: [] });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const prompt = h.$('[data-testid="input-textarea"]') as HTMLTextAreaElement;
    Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      "value",
    )!.set!.call(prompt, "start this elsewhere");
    prompt.dispatchEvent(new Event("input", { bubbles: true }));

    await h.click(".input-overflow-trigger");
    const sendToNew = h.$('[data-testid="send-to-new-session-btn"]') as HTMLButtonElement | null;
    expect(sendToNew).not.toBeNull();
    expect(sendToNew!.disabled).toBe(false);
    await h.click('[data-testid="send-to-new-session-btn"]');
    await h.flush();

    const modalPrompt = h.$(".ns-investigation-textarea") as HTMLTextAreaElement;
    expect(modalPrompt.value).toBe("start this elsewhere");
    expect(h.toJSON().input.text).toBe("");
    h.unmount();
  });

  it("renders selected file attachment on the optimistic user bubble", async () => {
    const session = makeSession({ id: "s-attach", messages: [] });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const prompt = h.$('[data-testid="input-textarea"]') as HTMLTextAreaElement;
    Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      "value",
    )!.set!.call(prompt, "read the attached file");
    prompt.dispatchEvent(new Event("input", { bubbles: true }));

    const inputs = h.$$('input[type="file"]') as HTMLInputElement[];
    const fileInput = inputs.find((input) => !input.getAttribute("accept"))!;
    const file = new File(["hello"], "local-note.txt", { type: "text/plain" });
    fireEvent.change(fileInput, { target: { files: [file] } });
    expect(await waitForSelector(h, ".file-preview-item")).not.toBeNull();

    await h.click('[data-testid="send-btn"]');

    expect(h.$('[data-testid="user-message"] .message-file-name')?.textContent).toBe("local-note.txt");
    expect(h.$('[data-testid="user-message"] .message-file-size')?.textContent).toBe("5 B");
    h.unmount();
  });

  it("queues a new session when REST fails before the WebSocket reports offline", async () => {
    const h = await renderApp({
      seed: {
        sessions: [],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    await clickNewSession(h);

    const prompt = h.$(".ns-investigation-textarea") as HTMLTextAreaElement;
    Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      "value",
    )!.set!.call(prompt, "queue during disconnect race");
    prompt.dispatchEvent(new Event("input", { bubbles: true }));
    await h.flush();

    h.backend.setOffline(true);
    await h.click(".modal-footer .btn-primary");

    expect(h.toJSON().sidebar.sessions).toHaveLength(1);
    expect(h.toJSON().chat.messages).toContainEqual(
      expect.objectContaining({
        status: "sending",
        text: expect.stringContaining("queue during disconnect race"),
      }),
    );
    await expect.poll(async () => (await loadOfflineActions()).length).toBe(1);
    h.unmount();
  });

  it("creates an empty session offline, then queues a prompt in it before sync", async () => {
    const h = await renderApp({
      seed: {
        sessions: [],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    h.dropConnection();
    h.backend.setOffline(true);
    await h.flush();
    await clickNewSession(h);
    await h.click(".modal-footer .btn-primary");

    expect(h.toJSON().sidebar.sessions).toHaveLength(1);
    await expect.poll(() => loadOfflineActions()).toEqual([
      expect.objectContaining({ type: "create_session", prompt: "" }),
    ]);

    await h.typeAndSend("prompt after empty offline session");
    expect(
      h.outbound.filter((frame) => frame.type === "send_message"),
    ).toHaveLength(0);
    await expect.poll(() => loadOfflineActions()).toEqual([
      expect.objectContaining({ type: "create_session", prompt: "" }),
      expect.objectContaining({
        prompt: "prompt after empty offline session",
        sendMode: "queue",
      }),
    ]);

    h.backend.setOffline(false);
    h.reopenConnection();
    await h.flush();

    expect(
      h.restCalls.filter((c) => c.method === "POST" && c.path === "/api/sessions"),
    ).toHaveLength(2);
    expect(h.outbound).toContainEqual(
      expect.objectContaining({
        type: "send_message",
        prompt: "prompt after empty offline session",
      }),
    );
    h.unmount();
  });

  it("queues a new session when create returns a retryable backend failure", async () => {
    const h = await renderApp({
      seed: {
        sessions: [],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    h.backend.failNextWithStatus(503, "/api/sessions", true);
    await clickNewSession(h);
    await h.click(".modal-footer .btn-primary");

    // The optimistic sidebar entry appears only after the durable-first
    // IndexedDB commit resolves, so both assertions are eventual.
    await expect.poll(() => h.toJSON().sidebar.sessions.length).toBe(1);
    await expect.poll(() => loadOfflineActions()).toEqual([
      expect.objectContaining({ type: "create_session" }),
    ]);
    h.unmount();
  });

  it("creates from the modal with default cwd before projects load", async () => {
    const session = makeSession({ id: "s1", cwd: "/tmp/cached-project" });
    localStorage.setItem("better-agent-selected-project", "/tmp/cached-project");
    const h = await renderApp({ seed: { sessions: [session], projects: [] } });
    await clickNewSession(h);
    await h.click(".modal-footer .btn-primary");

    expect(
      h.restCalls.find((c) => c.method === "POST" && c.path === "/api/sessions"),
    ).toEqual(expect.objectContaining({
      body: expect.objectContaining({ cwd: "/tmp/cached-project" }),
    }));
    h.unmount();
  });

  it("removes a durable queued prompt when the ack arrives through replay", async () => {
    const session = makeSession({
      id: "s1",
      messages: [
        makeUserMsg({
          content: "replayed durable prompt",
          client_id: "offline-client-1",
        }),
      ],
    });
    localStorage.setItem("better_agent_offline_queue", JSON.stringify([{
      sessionId: "s1",
      clientId: "offline-client-1",
      prompt: "replayed durable prompt",
      model: session.model,
      cwd: session.cwd,
      sendMode: "interrupt",
    }]));

    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession("s1");
    await h.flush();

    expect(localStorage.getItem("better_agent_offline_queue")).toBeNull();
    await expect.poll(async () => (await loadOfflineActions()).length).toBe(0);
    h.unmount();
  });

  it("persists an online prompt locally until backend acknowledgement", async () => {
    const session = makeSession({ id: "s1" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession("s1");
    await h.typeAndSend("durable before ack");

    const queue = await loadOfflineActions();
    expect(queue).toEqual([
      expect.objectContaining({
        sessionId: "s1",
        prompt: "durable before ack",
        sendMode: "queue",
      }),
    ]);
    expect(
      h.outbound.filter(
        (frame) => frame.type === "send_message" && frame.prompt === "durable before ack",
      ),
    ).toHaveLength(1);

    const sent = h.outbound.find(
      (frame) => frame.type === "send_message" && frame.prompt === "durable before ack",
    );
    h.emit({
      type: "user_message_persisted",
      data: {
        session_id: "s1",
        user_message: makeUserMsg({
          content: "durable before ack",
          client_id: sent!.client_id as string,
        }),
      },
    });
    await h.flush();

    await expect.poll(async () => (await loadOfflineActions()).length).toBe(0);
    h.unmount();
  });

  it("removes a durable prompt when the backend accepts it into the queue", async () => {
    const session = makeSession({ id: "s1" });
    localStorage.setItem("better_agent_offline_queue", JSON.stringify([{
      sessionId: "s1",
      clientId: "queued-client-1",
      prompt: "accepted into queue",
      model: session.model,
      cwd: session.cwd,
      sendMode: "queue",
    }]));

    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession("s1");
    h.emit({
      type: "user_message_queued",
      data: {
        app_session_id: "s1",
        lifecycle_msg_id: "life-queued-client-1",
        client_id: "queued-client-1",
        kind: "queued_behind",
      },
    });
    await h.flush();

    await expect.poll(async () => (await loadOfflineActions()).length).toBe(0);
    h.unmount();
  });

  it("removes a durable prompt when a queued prompt snapshot already contains it", async () => {
    const session = makeSession({
      id: "s1",
      queued_prompts: [{
        id: "queued-prompt-1",
        lifecycle_msg_id: "life-queued-client-1",
        client_id: "queued-client-1",
        content: "accepted into queue",
        kind: "queued_behind",
        queue_position: 1,
      }],
    });
    localStorage.setItem("better_agent_offline_queue", JSON.stringify([{
      sessionId: "s1",
      clientId: "queued-client-1",
      prompt: "accepted into queue",
      model: session.model,
      cwd: session.cwd,
      sendMode: "queue",
    }]));

    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession("s1");
    await h.flush();

    await expect.poll(async () => (await loadOfflineActions()).length).toBe(0);
    expect(
      h.outbound.filter(
        (frame) => frame.type === "send_message" && frame.client_id === "queued-client-1",
      ),
    ).toHaveLength(0);
    h.unmount();
  });

  it("does not render backend internal send rows as queued prompts", async () => {
    const session = makeSession({ id: "s1" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession("s1");

    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: "s1",
        patch: {
          queued_prompts: [{
            id: "internal-send",
            lifecycle_msg_id: "life-internal-send",
            content: "already sent",
            kind: "send",
            queue_position: 0,
          }],
        },
        originated_by: "OTHER_TAB",
      },
    });
    await h.flush();

    expect(h.$('[data-testid="queued-prompt-banner"]')).toBeNull();

    h.emit({
      type: "session_metadata_updated",
      data: {
        session_id: "s1",
        patch: {
          queued_prompts: [{
            id: "visible-queued",
            lifecycle_msg_id: "life-visible-queued",
            content: "actually queued",
            kind: "queued_behind",
            queue_position: 1,
          }],
        },
        originated_by: "OTHER_TAB",
      },
    });
    await h.flush();

    expect(h.$('[data-testid="queued-prompt-banner"]')?.textContent).toContain(
      "actually queued",
    );
    h.unmount();
  });

  it("clicking the row's × deletes the session and removes it from the sidebar", async () => {
    const a = makeSession({ id: "a" });
    const b = makeSession({ id: "b", name: "session b" });
    const h = await renderApp({ seed: { sessions: [a, b] } });
    await h.deleteSession("a");

    expect(
      h.restCalls.find((c) => c.method === "DELETE" && c.path === "/api/sessions/a"),
    ).toBeDefined();
    expect(h.toJSON().sidebar.sessions.map((s) => s.id)).toEqual(["b"]);
    h.unmount();
  });

  it("inline rename PUTs /api/sessions/:id/rename and updates the sidebar", async () => {
    const session = makeSession({ name: "old name" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.renameSession(session.id, "shiny new name");

    expect(
      h.restCalls.find(
        (c) => c.method === "PUT" && c.path === `/api/sessions/${session.id}/rename`,
      ),
    ).toBeDefined();
    expect(h.toJSON().sidebar.sessions[0].name).toContain("shiny new name");
    h.unmount();
  });

  it("Fork button POSTs /fork_and_send with the typed prompt once an agent sid exists", async () => {
    const session = makeSession({
      id: "parent",
      agent_session_id: "agent-sid-1",
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);
    const ta = h.$('[data-testid="input-textarea"]') as HTMLTextAreaElement;
    expect(ta).not.toBeNull();
    Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      "value",
    )!.set!.call(ta, "explore alternative");
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    await h.flush();
    await h.click(".input-overflow-trigger");
    const fork = h.$('[data-testid="fork-btn"]') as HTMLButtonElement | null;
    expect(fork).not.toBeNull();
    expect(fork!.disabled).toBe(false);
    await h.click('[data-testid="fork-btn"]');

    expect(
      h.restCalls.find(
        (c) => c.method === "POST" && c.path === `/api/sessions/parent/fork_and_send`,
      ),
    ).toBeDefined();
    h.unmount();
  });

  it("Fork button is disabled before any turn (no claude_sid)", async () => {
    const session = makeSession({ agent_session_id: null });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    const ta = h.$('[data-testid="input-textarea"]') as HTMLTextAreaElement;
    expect(ta).not.toBeNull();
    Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      "value",
    )!.set!.call(ta, "cannot fork yet");
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    await h.flush();
    await h.click(".input-overflow-trigger");

    const fork = h.$('[data-testid="fork-btn"]') as HTMLButtonElement | null;
    expect(fork).not.toBeNull();
    expect(fork!.disabled).toBe(true);
    expect(
      h.restCalls.find(
        (c) => c.method === "POST" && c.path === `/api/sessions/${session.id}/fork_and_send`,
      ),
    ).toBeUndefined();
    h.unmount();
  });

  it("subscribing on session select sends a subscribe frame", async () => {
    const session = makeSession();
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    expect(h.outbound).toContainEqual(
      expect.objectContaining({
        type: "subscribe",
        app_session_id: session.id,
      }),
    );
    h.unmount();
  });

  it("switching sessions sends unsubscribe(prev) + subscribe(next)", async () => {
    const a = makeSession({ id: "a", name: "A" });
    const b = makeSession({ id: "b", name: "B" });
    const h = await renderApp({ seed: { sessions: [a, b] } });

    await h.selectSession("a");
    await h.selectSession("b");

    const types = h.outbound.map((f) => `${f.type}:${f.app_session_id ?? ""}`);
    // unsubscribe for "a" must precede subscribe for "b"
    const idxUnsubA = types.indexOf("unsubscribe:a");
    const idxSubB = types.indexOf("subscribe:b");
    expect(idxUnsubA).toBeGreaterThanOrEqual(0);
    expect(idxSubB).toBeGreaterThan(idxUnsubA);
    h.unmount();
  });

  it("session switch clears optimistic pendingMessages from the chat view", async () => {
    const a = makeSession({ id: "a" });
    const b = makeSession({ id: "b", name: "B" });
    const h = await renderApp({ seed: { sessions: [a, b] } });
    await h.selectSession("a");
    await h.typeAndSend("queued on A");

    // The pending bubble is on screen for A.
    expect(
      h.toJSON().chat.messages.some((m) => m.status === "sending"),
    ).toBe(true);

    await h.selectSession("b");
    // Switching away clears pending.
    expect(h.toJSON().chat.messages.some((m) => m.status === "sending")).toBe(false);
    h.unmount();
  });
});
