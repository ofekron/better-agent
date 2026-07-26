import { afterEach, describe, expect, it, vi } from "vitest";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import type { UserInteractionRequest, WSEvent } from "../src/types";

function approval(sessionId: string, requestId = "approval-remote"): UserInteractionRequest {
  return {
    request_id: requestId,
    app_session_id: sessionId,
    kind: "approval",
    prompt: "Deploy the release?",
    status: "pending",
    created_at: 1,
  };
}

function inputRequest(sessionId: string, requestId = "input-remote"): UserInteractionRequest {
  return {
    request_id: requestId,
    app_session_id: sessionId,
    kind: "input",
    questions: [{
      id: "scope",
      header: "Scope",
      question: "Include documentation?",
      options: [
        { label: "Yes", description: "Update the guide" },
        { label: "No", description: "Code only" },
      ],
    }],
    status: "pending",
    created_at: 1,
  };
}

afterEach(() => {
  Object.defineProperty(window, "pywebview", { value: undefined, configurable: true });
});

describe("cross-session user request toast", () => {
  it("expands and resolves an input request without leaving the current session", async () => {
    const current = makeSession({ id: "current", name: "Current work" });
    const source = makeSession({ id: "source", name: "Release work" });
    const request = inputRequest(source.id);
    const h = await renderApp({
      seed: { sessions: [current, source], userInputs: [request] },
    });
    await h.selectSession(current.id);
    await h.flush();

    const toastSelector = '[data-testid="user-request-toast"][data-session-id="source"]';
    expect(h.$(`${toastSelector} .user-request-toast__response`)?.hasAttribute("hidden")).toBe(true);

    await h.click(`${toastSelector} [data-action="respond-in-place"]`);
    expect(h.$(`${toastSelector} .user-request-toast__response`)?.hasAttribute("hidden")).toBe(false);

    await h.click(`${toastSelector} .user-input-card__option:nth-child(2) input[type="radio"]`);
    await h.click(`${toastSelector} [data-action="respond-in-place"]`);
    await h.click(`${toastSelector} [data-action="respond-in-place"]`);
    await h.click(`${toastSelector} .user-input-card__actions .primary`);

    expect(h.$(toastSelector)).toBeNull();
    expect(h.$('[data-testid="user-input-card"]')).toBeNull();
    expect(h.restCalls).toContainEqual(expect.objectContaining({
      method: "POST",
      path: `/api/user-input/${request.request_id}/resolve`,
      body: { app_session_id: source.id, answers: { scope: "No" } },
    }));
    h.unmount();
  });

  it("links a background request to its interactive session", async () => {
    const current = makeSession({ id: "current", name: "Current work" });
    const source = makeSession({ id: "source", name: "Release work" });
    const h = await renderApp({
      seed: { sessions: [current, source], userInputs: [approval(source.id)] },
    });
    await h.selectSession(current.id);
    await h.flush();

    const toast = h.$('[data-testid="user-request-toast"][data-session-id="source"]');
    expect(toast?.textContent).toContain("Deploy the release?");
    expect(toast?.textContent).toContain("Release work");

    await h.click('[data-testid="user-request-toast"][data-session-id="source"] [data-action="open-session"]');
    expect(h.$('[data-testid="user-request-toast"][data-session-id="source"]')).toBeNull();
    expect(h.$('[data-testid="user-approval-card"]')?.textContent).toContain("Deploy the release?");
    h.unmount();
  });

  it("adds and clears a background toast from websocket lifecycle events", async () => {
    const current = makeSession({ id: "current" });
    const source = makeSession({ id: "source" });
    const request = approval(source.id, "approval-live");
    const h = await renderApp({ seed: { sessions: [current, source] } });
    await h.selectSession(current.id);

    const notifyUser = vi.fn().mockResolvedValue({ success: true });
    Object.defineProperty(window, "pywebview", {
      value: { api: { notify_user: notifyUser } },
      configurable: true,
    });
    h.backend.state.userInputs.push(request);
    h.emit({
      type: "session_user_input_changed",
      data: { session_id: source.id, pending_user_input_count: 1 },
    } as WSEvent);
    await h.flush();
    expect(h.$('[data-testid="user-request-toast"][data-session-id="source"]')).not.toBeNull();
    expect(notifyUser).toHaveBeenCalledWith("userApproval.title", "Deploy the release?");

    h.emit({
      type: "user_input_resolved",
      data: { request_id: request.request_id, app_session_id: source.id },
    } as WSEvent);
    await h.flush();
    expect(h.$('[data-testid="user-request-toast"][data-session-id="source"]')).toBeNull();
    h.unmount();
  });

  // A request raised while this client is disconnected reaches nobody:
  // `user_input_requested` is session-scoped and
  // `session_user_input_changed` is fire-and-forget, and neither is
  // replayed on reconnect. Only a snapshot re-pull recovers it — without
  // one the card stays invisible until a full page reload.
  it("recovers a request created while the socket was down", async () => {
    const current = makeSession({ id: "current" });
    const source = makeSession({ id: "source" });
    const h = await renderApp({ seed: { sessions: [current, source] } });
    await h.selectSession(current.id);
    await h.flush();

    h.dropConnection();
    await h.flush();
    // Created during the disconnect window: the store has it, but no
    // event is delivered to this client.
    h.backend.state.userInputs.push(approval(source.id, "approval-offline"));
    await h.flush();
    expect(h.$('[data-testid="user-request-toast"][data-session-id="source"]')).toBeNull();

    h.reopenConnection();
    await h.flush();
    expect(h.$('[data-testid="user-request-toast"][data-session-id="source"]')).not.toBeNull();
    h.unmount();
  });

  // The snapshot validator must accept every kind the store emits.
  // Dropping `memory` made the reconnect/ping re-pull delete a
  // memory-proposal card that the live WS event had just rendered.
  it("keeps a memory proposal across a snapshot re-pull", async () => {
    const current = makeSession({ id: "current" });
    const source = makeSession({ id: "source" });
    const request: UserInteractionRequest = {
      request_id: "memory-live",
      app_session_id: source.id,
      kind: "memory",
      memory_proposal: {
        action: "add",
        name: "deploy-flow",
        description: "How deploys are promoted",
        type: "project",
        content: "dev -> qa -> main",
        scope_type: "global",
        scope_path: "",
      },
      status: "pending",
      created_at: 1,
    } as UserInteractionRequest;
    const h = await renderApp({ seed: { sessions: [current, source], userInputs: [request] } });
    await h.selectSession(current.id);
    await h.flush();
    expect(h.$('[data-testid="user-request-toast"][data-session-id="source"]')).not.toBeNull();

    h.reopenConnection();
    await h.flush();
    expect(h.$('[data-testid="user-request-toast"][data-session-id="source"]')).not.toBeNull();
    h.unmount();
  });

  // The reconnect re-pull and an incoming request collide by definition —
  // reconnect is exactly when queued events arrive. Neither may erase the
  // other.
  it("keeps both when a request lands during the reconnect re-pull", async () => {
    const current = makeSession({ id: "current" });
    const source = makeSession({ id: "source" });
    const offline = approval(source.id, "approval-offline");
    const live = inputRequest(source.id, "input-live");
    const h = await renderApp({ seed: { sessions: [current, source] } });
    await h.selectSession(current.id);
    await h.flush();

    h.backend.state.userInputs.push(offline);
    const releaseSnapshot = h.backend.holdNext("GET", "/api/user-input/pending");
    h.reopenConnection();
    await h.flush();
    h.emit({ type: "user_input_requested", data: live } as WSEvent);
    await h.flush();
    releaseSnapshot();
    await h.flush();

    expect(h.$(`[data-testid="user-request-toast"][data-request-id="${offline.request_id}"]`)).not.toBeNull();
    expect(h.$(`[data-testid="user-request-toast"][data-request-id="${live.request_id}"]`)).not.toBeNull();
    h.unmount();
  });

  // Mirror of the case above: the snapshot was taken while the request
  // was still pending, so adopting it blindly would resurrect a card the
  // user already answered — and let them answer it twice.
  it("does not resurrect a request resolved during the reconnect re-pull", async () => {
    const current = makeSession({ id: "current" });
    const source = makeSession({ id: "source" });
    const request = approval(source.id, "approval-resolved-midflight");
    const h = await renderApp({ seed: { sessions: [current, source], userInputs: [request] } });
    await h.selectSession(current.id);
    await h.flush();
    expect(h.$('[data-testid="user-request-toast"][data-session-id="source"]')).not.toBeNull();

    const releaseSnapshot = h.backend.holdNext("GET", "/api/user-input/pending");
    h.reopenConnection();
    await h.flush();
    h.emit({
      type: "user_input_resolved",
      data: { request_id: request.request_id, app_session_id: source.id },
    } as WSEvent);
    await h.flush();
    releaseSnapshot();
    await h.flush();

    expect(h.$('[data-testid="user-request-toast"][data-session-id="source"]')).toBeNull();
    h.unmount();
  });

  it("keeps a live request when an older snapshot finishes later", async () => {
    const current = makeSession({ id: "current" });
    const source = makeSession({ id: "source" });
    const request = approval(source.id, "approval-race");
    const h = await renderApp({ seed: { sessions: [current, source] } });
    await h.selectSession(current.id);

    const releaseSnapshot = h.backend.holdNext("GET", "/api/user-input/pending");
    h.emit({
      type: "session_user_input_changed",
      data: { session_id: source.id, pending_user_input_count: 1 },
    } as WSEvent);
    await h.flush();
    h.emit({ type: "user_input_requested", data: request } as WSEvent);
    await h.flush();
    releaseSnapshot();
    await h.flush();

    expect(h.$('[data-testid="user-request-toast"][data-session-id="source"]')).not.toBeNull();
    h.unmount();
  });
});
