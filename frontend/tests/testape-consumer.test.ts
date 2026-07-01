// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  extractVisibleChatPanelTree,
  publishBetterAgentTestApeState,
  publishBetterAgentVisibleChatPanelTree,
} from "src/lib/testapeConsumer";
import type { Session } from "src/types";

function session(overrides: Partial<Session> = {}): Session {
  return {
    id: "s1",
    name: "Main",
    cwd: "/repo",
    model: "sonnet",
    orchestration_mode: "native",
    messages: [{ id: "m1", role: "user", content: "hi" }],
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  } as Session;
}

describe("Better Agent TestApe consumer", () => {
  afterEach(() => {
    delete window.testape;
    document.body.innerHTML = "";
  });

  it("publishes Better Agent state through the generic TestApe web SDK", () => {
    const sendState = vi.fn();
    window.testape = { sendState };

    publishBetterAgentTestApeState({
      authStatus: "authed",
      connected: true,
      viewport: "desktop",
      sessions: [session()],
      currentSession: session({
        provider_id: "claude",
        open_file_panels: [{ id: "p1", path: "/repo/src/App.tsx" }],
      }),
      openSessionIds: ["s1"],
      pendingMessageCount: 2,
      queuedPromptCount: 1,
      rightPanelOpen: true,
      rightPanelTab: "files",
    });

    expect(sendState).toHaveBeenCalledWith("better_agent_auth_status", "authed");
    expect(sendState).toHaveBeenCalledWith("better_agent_backend_connected", true);
    expect(sendState).toHaveBeenCalledWith("better_agent_viewport", "desktop");
    expect(sendState).toHaveBeenCalledWith("better_agent_session_count", 1);
    expect(sendState).toHaveBeenCalledWith("better_agent_current_session", {
      id: "s1",
      name: "Main",
      cwd: "/repo",
      provider_id: "claude",
      model: "sonnet",
      orchestration_mode: "native",
      message_count: 1,
    });
    expect(sendState).toHaveBeenCalledWith("better_agent_current_session_id", "s1");
    expect(sendState).toHaveBeenCalledWith("better_agent_open_session_ids", ["s1"]);
    expect(sendState).toHaveBeenCalledWith("better_agent_pending_message_count", 2);
    expect(sendState).toHaveBeenCalledWith("better_agent_queued_prompt_count", 1);
    expect(sendState).toHaveBeenCalledWith("better_agent_right_panel", { open: true, tab: "files" });
    expect(sendState).toHaveBeenCalledWith("better_agent_native_file_panel_count", 1);
    expect(sendState).toHaveBeenCalledWith("better_agent_native_file_panel_paths", ["/repo/src/App.tsx"]);
  });

  it("extracts the visible linear chat panel tree on demand", () => {
    document.body.innerHTML = `
      <div class="chat-toolbar-title">Main</div>
      <div data-testid="chat-messages">
        <div data-testid="user-message" data-message-id="u1">
          <div class="message-box-body">Hello</div>
        </div>
        <div class="message user-message" data-message-id="u2">
          <div class="message-content">Standalone</div>
        </div>
        <div data-testid="assistant-message" data-message-id="a1">
          <div class="message-content">Answer</div>
        </div>
      </div>
    `;
    window.history.replaceState({}, "", "/s/s1");

    expect(extractVisibleChatPanelTree()).toEqual({
      visible: true,
      session_id: "s1",
      title: "Main",
      regions: [
        {
          kind: "linear",
          session_id: "s1",
          messages: [
            { id: "u1", role: "user", text: "Hello" },
            { id: "u2", role: "user", text: "Standalone" },
            { id: "a1", role: "assistant", text: "Answer" },
          ],
        },
      ],
    });
  });

  it("publishes the visible chat panel tree through the TestApe SDK on demand", () => {
    const sendState = vi.fn();
    window.testape = { sendState };
    document.body.innerHTML = `
      <div data-testid="chat-messages">
        <div data-testid="user-message" data-message-id="u1">
          <div class="message-box-body">Hello</div>
        </div>
      </div>
    `;

    const tree = publishBetterAgentVisibleChatPanelTree();

    expect(tree?.regions[0]?.messages).toEqual([{ id: "u1", role: "user", text: "Hello" }]);
    expect(sendState).toHaveBeenCalledWith("better_agent_visible_chat_panel_tree", tree);
    expect(window.__betterAgentTestApe?.extractVisibleChatPanelTree()).toEqual(tree);
  });

  it("extracts fork shared and pane regions with session ids", () => {
    document.body.innerHTML = `
      <div data-testid="chat-messages">
        <div data-testid="fork-shared">
          <div data-testid="user-message" data-message-id="u1">
            <div class="message-box-body">Shared</div>
          </div>
        </div>
        <div data-testid="fork-grid">
          <div data-testid="fork-pane" data-session-id="root" class="fork-pane-focused">
            <div data-testid="assistant-message" data-message-id="a-root">
              <div class="message-content">Root answer</div>
            </div>
          </div>
          <div data-testid="fork-pane" data-session-id="fork">
            <div data-testid="assistant-message" data-message-id="a-fork">
              <div class="message-content">Fork answer</div>
            </div>
          </div>
        </div>
      </div>
    `;
    window.history.replaceState({}, "", "/s/root");

    expect(extractVisibleChatPanelTree().regions).toEqual([
      {
        kind: "fork_shared",
        session_id: "root",
        messages: [{ id: "u1", role: "user", text: "Shared" }],
      },
      {
        kind: "fork_pane",
        session_id: "root",
        focused: true,
        messages: [{ id: "a-root", role: "assistant", text: "Root answer" }],
      },
      {
        kind: "fork_pane",
        session_id: "fork",
        focused: false,
        messages: [{ id: "a-fork", role: "assistant", text: "Fork answer" }],
      },
    ]);
  });
});
