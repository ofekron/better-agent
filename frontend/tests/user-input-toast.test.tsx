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

afterEach(() => {
  Object.defineProperty(window, "pywebview", { value: undefined, configurable: true });
});

describe("cross-session user request toast", () => {
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
