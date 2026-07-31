import { describe, it, expect } from "vitest";
import { fireEvent } from "@testing-library/react";
import { renderApp } from "./harness";
import { makeSession, makeUserMsg } from "./fixtures";

const NEW_SESSION_DEFAULTS_KEY = "better-agent-new-session-defaults";

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

async function acknowledgeSentPrompt(
  h: Awaited<ReturnType<typeof renderApp>>,
  sent: NonNullable<Awaited<ReturnType<typeof waitForSend>>>,
  content: string,
) {
  h.emit({
    type: "user_message_persisted",
    data: {
      session_id: sent.app_session_id,
      user_message: makeUserMsg({
        content,
        client_id: sent.client_id as string,
      }),
    },
  });
  await h.flush();
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

async function chooseNewSessionAction(
  h: Awaited<ReturnType<typeof renderApp>>,
  action: RegExp,
) {
  await h.click(".ns-create-toggle");
  await h.clickByText(action);
}

function setNewSessionActionDefault(action: "create" | "send" | "send-and-open") {
  const previous = localStorage.getItem(NEW_SESSION_DEFAULTS_KEY);
  localStorage.setItem(NEW_SESSION_DEFAULTS_KEY, JSON.stringify({ creationAction: action }));
  return () => {
    if (previous === null) {
      localStorage.removeItem(NEW_SESSION_DEFAULTS_KEY);
      return;
    }
    localStorage.setItem(NEW_SESSION_DEFAULTS_KEY, previous);
  };
}

async function enterNewSessionPrompt(
  h: Awaited<ReturnType<typeof renderApp>>,
  text: string,
) {
  const prompt = h.$(".ns-investigation-textarea") as HTMLTextAreaElement;
  fireEvent.input(prompt, { target: { value: text } });
  await h.flush();
}

async function pressEnterInNewSessionPrompt(
  h: Awaited<ReturnType<typeof renderApp>>,
  init?: KeyboardEventInit,
) {
  const defaultAllowed = fireEvent.keyDown(h.$(".ns-investigation-textarea") as HTMLTextAreaElement, {
    key: "Enter",
    code: "Enter",
    charCode: 13,
    ...init,
  });
  await h.flush();
  return defaultAllowed;
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
        path: `/api/sessions/${h.createdSessionId()}/selectors`,
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

  it("Create stores the initial prompt as a draft and keeps the current session open", async () => {
    const current = makeSession({ id: "current", cwd: "/tmp/project" });
    const h = await renderApp({
      seed: {
        sessions: [current],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    await h.selectSession(current.id);
    await clickNewSession(h);
    await enterNewSessionPrompt(h, "draft this in the new session");
    await chooseNewSessionAction(h, /^(Create|newSession\.create)$/);
    await h.flush();

    expect(window.location.pathname).toBe("/s/current");
    expect(await waitForSend(h, "draft this in the new session")).toBeUndefined();
    const draftedId = h.createdSessionId();
    expect(h.restCalls).toContainEqual(expect.objectContaining({
      method: "PATCH",
      path: `/api/sessions/${draftedId}/draft`,
      body: expect.objectContaining({ draft_input: "draft this in the new session" }),
    }));
    expect(h.backend.state.sessions.find((session) => session.id === draftedId)?.draft_input)
      .toBe("draft this in the new session");
    h.unmount();
  });

  it("Create & Send dispatches the initial prompt and keeps the current session open", async () => {
    const current = makeSession({ id: "current", cwd: "/tmp/project" });
    const h = await renderApp({
      seed: {
        sessions: [current],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    await h.selectSession(current.id);
    await clickNewSession(h);
    await enterNewSessionPrompt(h, "send this in the background");
    await chooseNewSessionAction(h, /^(Create & Send|newSession\.createAndSend)$/);
    await h.flush();

    expect(window.location.pathname).toBe("/s/current");
    expect(await waitForSend(h, "send this in the background")).toEqual(
      expect.objectContaining({ app_session_id: h.createdSessionId() }),
    );
    h.unmount();
  });

  it("retries a failed create under the same id instead of orphaning an empty session", async () => {
    // A create that times out or 5xxes may still have landed server-side.
    // Replaying it under a fresh id left the landed session behind as an
    // empty "Session HH:MM" and put the prompt in a second session.
    const current = makeSession({ id: "current", cwd: "/tmp/project" });
    const h = await renderApp({
      seed: {
        sessions: [current],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    await h.selectSession(current.id);
    h.backend.failNextWithStatus(503, "/api/sessions");
    await clickNewSession(h);
    await enterNewSessionPrompt(h, "must not spawn two sessions");
    await chooseNewSessionAction(h, /^(Create & Send|newSession\.createAndSend)$/);
    await h.flush();

    const attemptedId = h.createdSessionId(0);
    const [queued] = JSON.parse(localStorage.getItem("better_agent_offline_queue") || "[]");
    expect(queued).toEqual(expect.objectContaining({
      type: "create_session",
      session: expect.objectContaining({ id: attemptedId }),
    }));

    for (let i = 0; i < 10 && localStorage.getItem("better_agent_offline_queue"); i++) {
      await h.flush();
    }

    const createIds = h.restCalls
      .filter((c) => c.method === "POST" && c.path === "/api/sessions")
      .map((c) => (c.body as { client_session_id?: string }).client_session_id);
    expect(new Set(createIds)).toEqual(new Set([attemptedId]));
    expect(
      h.backend.state.sessions.filter((session) => session.id !== current.id),
    ).toHaveLength(1);
    h.unmount();
  });

  it("Create preserves an offline draft without switching sessions or sending it", async () => {
    const current = makeSession({ id: "current", cwd: "/tmp/project" });
    const h = await renderApp({
      seed: {
        sessions: [current],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    await h.selectSession(current.id);
    h.dropConnection();
    h.backend.setOffline(true);
    await h.flush();
    await clickNewSession(h);
    await enterNewSessionPrompt(h, "keep this offline draft");
    await chooseNewSessionAction(h, /^(Create|newSession\.create)$/);
    await h.flush();

    const [queued] = JSON.parse(localStorage.getItem("better_agent_offline_queue") || "[]");
    expect(window.location.pathname).toBe("/s/current");
    expect(queued).toEqual(expect.objectContaining({
      type: "create_session",
      prompt: "",
      session: expect.objectContaining({ draft_input: "keep this offline draft" }),
    }));

    h.backend.setOffline(false);
    h.reopenConnection();
    for (let i = 0; i < 10 && localStorage.getItem("better_agent_offline_queue"); i++) {
      await h.flush();
    }

    expect(window.location.pathname).toBe("/s/current");
    expect(h.outbound.some((frame) => frame.type === "send_message")).toBe(false);
    expect(h.backend.state.sessions.find((session) => session.id === queued.session.id)?.draft_input)
      .toBe("keep this offline draft");
    expect(localStorage.getItem("better_agent_offline_queue")).toBeNull();
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

    const createdId = h.createdSessionId();
    const detailGetIndex = h.restCalls.findIndex(
      (c) => c.method === "GET" && c.path === `/api/sessions/${createdId}`,
    );
    const subscribeIndex = h.outbound.findIndex(
      (frame) =>
        frame.type === "subscribe" &&
        frame.app_session_id === createdId,
    );

    expect(window.location.pathname).toBe(`/s/${createdId}`);
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

    const filteredOutId = h.createdSessionId();
    expect(window.location.pathname).toBe(`/s/${filteredOutId}`);
    expect(h.restCalls).toContainEqual(
      expect.objectContaining({ method: "GET", path: `/api/sessions/${filteredOutId}` }),
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
      data: { session_id: h.createdSessionId(), name: "AI titled session" },
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
    expect(localStorage.getItem("better_agent_offline_queue")).toBeNull();
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
    expect(JSON.parse(localStorage.getItem("better_agent_offline_queue") || "[]")).toHaveLength(1);

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
    expect(JSON.parse(localStorage.getItem("better_agent_offline_queue") || "[]")).toHaveLength(1);

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

    expect(localStorage.getItem("better_agent_offline_queue")).toBeNull();
    h.unmount();
  });

  it("pressing Enter in the new-session initial prompt runs the selected Create action", async () => {
    const restoreDefault = setNewSessionActionDefault("create");
    const current = makeSession({ id: "current", cwd: "/tmp/project" });
    const h = await renderApp({
      seed: {
        sessions: [current],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    await h.selectSession(current.id);
    await clickNewSession(h);
    await enterNewSessionPrompt(h, "draft from enter");
    await pressEnterInNewSessionPrompt(h);

    const draftedId = h.createdSessionId();
    expect(window.location.pathname).toBe("/s/current");
    expect(await waitForSend(h, "draft from enter")).toBeUndefined();
    expect(h.backend.state.sessions.find((session) => session.id === draftedId)?.draft_input)
      .toBe("draft from enter");
    h.unmount();
    restoreDefault();
  });

  it("pressing Enter in the new-session initial prompt runs the selected Create & Send action", async () => {
    const restoreDefault = setNewSessionActionDefault("send");
    const current = makeSession({ id: "current", cwd: "/tmp/project" });
    const h = await renderApp({
      seed: {
        sessions: [current],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    await h.selectSession(current.id);
    await clickNewSession(h);
    await enterNewSessionPrompt(h, "create and send from enter");
    await pressEnterInNewSessionPrompt(h);

    expect(
      h.restCalls.filter((c) => c.method === "POST" && c.path === "/api/sessions"),
    ).toHaveLength(1);
    expect(
      h.restCalls.find((c) => c.method === "POST" && c.path === "/api/sessions"),
    ).toEqual(expect.objectContaining({
      credentials: "include",
      body: expect.objectContaining({ cwd: "/tmp/project" }),
    }));
    const sent = await waitForSend(h, "create and send from enter");
    expect(sent).toEqual(expect.objectContaining({ type: "send_message" }));
    const subscribeIndex = h.outbound.findIndex(
      (frame) =>
        frame.type === "subscribe" &&
        frame.app_session_id === sent?.app_session_id,
    );
    const sendIndex = h.outbound.indexOf(sent!);
    expect(subscribeIndex).toBeGreaterThan(-1);
    expect(subscribeIndex).toBeLessThan(sendIndex);
    await acknowledgeSentPrompt(h, sent!, "create and send from enter");
    h.unmount();
    restoreDefault();
  });

  it("pressing Enter in the new-session initial prompt runs the selected Create & Send & Open action", async () => {
    const restoreDefault = setNewSessionActionDefault("send-and-open");
    const current = makeSession({ id: "current", cwd: "/tmp/project" });
    const h = await renderApp({
      seed: {
        sessions: [current],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    await h.selectSession(current.id);
    await clickNewSession(h);
    await enterNewSessionPrompt(h, "create send and open from enter");
    await pressEnterInNewSessionPrompt(h);

    const createdId = h.createdSessionId();
    expect(window.location.pathname).toBe(`/s/${createdId}`);
    const sent = await waitForSend(h, "create send and open from enter");
    expect(sent).toEqual(
      expect.objectContaining({ app_session_id: createdId }),
    );
    await acknowledgeSentPrompt(h, sent!, "create send and open from enter");
    h.unmount();
    restoreDefault();
  });

  it("keeps multiline and IME initial prompt Enter paths from creating a session", async () => {
    const restoreDefault = setNewSessionActionDefault("send-and-open");
    const current = makeSession({ id: "current", cwd: "/tmp/project" });
    const h = await renderApp({
      seed: {
        sessions: [current],
        projects: [{
          path: "/tmp/project",
          name: "project",
          created_at: new Date().toISOString(),
          last_used: new Date().toISOString(),
        }],
      },
    });
    await h.selectSession(current.id);
    await clickNewSession(h);
    await enterNewSessionPrompt(h, "do not create");
    await pressEnterInNewSessionPrompt(h, { shiftKey: true });
    await pressEnterInNewSessionPrompt(h, { isComposing: true });

    expect(
      h.restCalls.filter((c) => c.method === "POST" && c.path === "/api/sessions"),
    ).toHaveLength(0);
    expect(h.outbound.some((frame) => frame.type === "send_message")).toBe(false);
    h.unmount();
    restoreDefault();
  });

  it("keeps disabled initial prompt Enter from creating a session", async () => {
    const restoreDefault = setNewSessionActionDefault("send-and-open");
    const h = await renderApp({
      seed: { sessions: [], projects: [] },
    });
    await clickNewSession(h);
    await enterNewSessionPrompt(h, "do not create");
    await expect(pressEnterInNewSessionPrompt(h)).resolves.toBe(false);

    expect(
      h.restCalls.filter((c) => c.method === "POST" && c.path === "/api/sessions"),
    ).toHaveLength(0);
    expect(h.outbound.some((frame) => frame.type === "send_message")).toBe(false);
    h.unmount();
    restoreDefault();
  });

  it("keeps a deferred new-session prompt durable across reload and pre-ack replay", async () => {
    const promptText = "survive the new-session handoff";
    const current = makeSession({ id: "current", cwd: "/tmp/project" });
    const projects = [{
      path: "/tmp/project",
      name: "project",
      created_at: new Date().toISOString(),
      last_used: new Date().toISOString(),
    }];
    const first = await renderApp({
      seed: { sessions: [current], projects },
    });
    await first.selectSession(current.id);
    // The new session's id is minted client-side, so hold the first detail
    // GET for any uuid-shaped id (static subpaths like /summaries must not
    // swallow the hold).
    const releaseDetail = first.backend.holdNext(
      "GET",
      /^\/api\/sessions\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    );
    await clickNewSession(first);
    await enterNewSessionPrompt(first, promptText);
    await first.click(".modal-footer .btn-primary");
    await first.flush();

    expect(
      first.outbound.some(
        (frame) => frame.type === "send_message" && frame.prompt === promptText,
      ),
    ).toBe(false);
    const [durable] = JSON.parse(
      localStorage.getItem("better_agent_offline_queue") || "[]",
    );
    const deferredId = first.createdSessionId();
    expect(durable).toEqual(expect.objectContaining({
      sessionId: deferredId,
      prompt: promptText,
      deferUntilTargetReady: true,
    }));
    const clientId = durable.clientId as string;
    const created = first.backend.state.sessions.find((session) => session.id === deferredId)!;
    first.unmount();
    releaseDetail();

    const second = await renderApp({
      seed: { sessions: [current, created], projects },
    });
    const firstReplay = await waitForSend(second, promptText);
    expect(firstReplay).toEqual(expect.objectContaining({
      app_session_id: deferredId,
      client_id: clientId,
    }));
    const subscribeIndex = second.outbound.findIndex(
      (frame) =>
        frame.type === "subscribe" &&
        frame.app_session_id === deferredId,
    );
    expect(subscribeIndex).toBeLessThan(second.outbound.indexOf(firstReplay!));
    expect(JSON.parse(
      localStorage.getItem("better_agent_offline_queue") || "[]",
    )).toHaveLength(1);
    second.unmount();

    const third = await renderApp({
      seed: { sessions: [current, created], projects },
    });
    const secondReplay = await waitForSend(third, promptText);
    expect(secondReplay).toEqual(expect.objectContaining({
      app_session_id: deferredId,
      client_id: clientId,
    }));
    third.emit({
      type: "user_message_persisted",
      data: {
        session_id: deferredId,
        user_message: makeUserMsg({
          content: promptText,
          client_id: clientId,
        }),
      },
    });
    await third.flush();

    expect(localStorage.getItem("better_agent_offline_queue")).toBeNull();
    third.unmount();
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
    const session = makeSession({ id: "source", messages: [] });
    const h = await renderApp({
      seed: {
        sessions: [session],
        projects: [{
          path: "/tmp/project",
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
        app_session_id: h.createdSessionId(),
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
    expect(JSON.parse(localStorage.getItem("better_agent_offline_queue") || "[]")).toHaveLength(1);
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
    expect(JSON.parse(localStorage.getItem("better_agent_offline_queue") || "[]")).toEqual([
      expect.objectContaining({ type: "create_session", prompt: "" }),
    ]);

    await h.typeAndSend("prompt after empty offline session");
    expect(
      h.outbound.filter((frame) => frame.type === "send_message"),
    ).toHaveLength(0);
    expect(JSON.parse(localStorage.getItem("better_agent_offline_queue") || "[]")).toEqual([
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

  it("keeps a new-session modal available while an offline create syncs", async () => {
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

    const releaseCreate = h.backend.holdNext("POST", "/api/sessions");
    h.backend.setOffline(false);
    h.reopenConnection();
    await h.flush();
    await clickNewSession(h);

    const createButton = h.$(".ns-create-primary") as HTMLButtonElement;
    expect(createButton.disabled).toBe(false);
    expect(createButton.dataset.progressInflight).toBeUndefined();

    releaseCreate();
    await h.flush();
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

    expect(h.toJSON().sidebar.sessions).toHaveLength(1);
    expect(JSON.parse(localStorage.getItem("better_agent_offline_queue") || "[]")).toEqual([
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
    h.unmount();
  });

  it("persists an online prompt locally until backend acknowledgement", async () => {
    const session = makeSession({ id: "s1" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession("s1");
    await h.typeAndSend("durable before ack");

    const queue = JSON.parse(localStorage.getItem("better_agent_offline_queue") || "[]");
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

    expect(localStorage.getItem("better_agent_offline_queue")).toBeNull();
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

    expect(localStorage.getItem("better_agent_offline_queue")).toBeNull();
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
      expect.objectContaining({ type: "subscribe", app_session_id: session.id }),
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
    // Auto-select (src/autoSelectSession.ts) may subscribe to a remembered
    // session on mount before either explicit selectSession call, so
    // "unsubscribe:a" must precede the LAST "subscribe:b" (the one from
    // switching back to b), not necessarily the first.
    const idxUnsubA = types.indexOf("unsubscribe:a");
    const idxSubB = types.lastIndexOf("subscribe:b");
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
