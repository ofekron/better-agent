// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  extractVisibleChatPanelTree,
  publishBetterAgentTestApeState,
  publishBetterAgentVisibleChatPanelTree,
} from "src/lib/testapeConsumer";
import { compareRenderedTreeToSession } from "src/lib/staleViewDetector";
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
        <div data-testid="assistant-message" data-message-id="a1" data-canonical-message-text="Answer">
          <div class="message-content">
            <div class="message-box open">
              <div class="message-box-body">Earlier event details</div>
            </div>
            <div class="message-box open">
              <button class="message-box-toggle">▼</button>
              <div class="message-box-body">Answer</div>
            </div>
          </div>
        </div>
      </div>
    `;
    window.history.replaceState({}, "", "/s/s1");

    const tree = extractVisibleChatPanelTree();
    expect(tree).toEqual({
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
    expect(compareRenderedTreeToSession(tree, session({
      messages: [
        { id: "u1", role: "user", content: "Hello" },
        { id: "u2", role: "user", content: "Standalone" },
        { id: "a1", role: "assistant", content: "Answer" },
      ],
    }) as Session).mismatches).toEqual([]);
  });

  it("extracts full collapsed assistant text instead of its one-line preview", () => {
    document.body.innerHTML = `
      <div data-testid="chat-messages">
        <div data-testid="assistant-message" data-message-id="a1" data-canonical-message-text="First line&#10;Second line">
          <div class="message-content">
            <div class="message-box">
              <button class="message-box-toggle">▶</button>
              <button class="message-box-collapsed-body">First line…</button>
            </div>
          </div>
        </div>
      </div>
    `;

    expect(extractVisibleChatPanelTree().regions[0]?.messages).toEqual([
      { id: "a1", role: "assistant", text: "First line Second line" },
    ]);
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

  it("excludes horizontally clipped fork panes from the visible tree", () => {
    document.body.innerHTML = `
      <div data-testid="chat-messages">
        <div data-testid="fork-grid">
          <div data-testid="fork-pane" data-session-id="onscreen">
            <div data-testid="user-message" data-message-id="u1">
              <div class="message-box-body">Visible</div>
            </div>
          </div>
          <div data-testid="fork-pane" data-session-id="offscreen">
            <div data-testid="user-message" data-message-id="u2">
              <div class="message-box-body">Hidden</div>
            </div>
          </div>
        </div>
      </div>
    `;
    const chat = document.querySelector<HTMLElement>('[data-testid="chat-messages"]')!;
    const [onscreen, offscreen] = Array.from(
      document.querySelectorAll<HTMLElement>('[data-testid="fork-pane"]'),
    );
    const visibleMessage = document.querySelector<HTMLElement>('[data-message-id="u1"]')!;
    vi.spyOn(chat, "getBoundingClientRect").mockReturnValue({
      top: 0,
      bottom: 500,
      left: 0,
      right: 500,
      width: 500,
      height: 500,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });
    vi.spyOn(onscreen, "getBoundingClientRect").mockReturnValue({
      top: 10,
      bottom: 200,
      left: 10,
      right: 250,
      width: 240,
      height: 190,
      x: 10,
      y: 10,
      toJSON: () => ({}),
    });
    vi.spyOn(visibleMessage, "getBoundingClientRect").mockReturnValue({
      top: 20,
      bottom: 100,
      left: 20,
      right: 200,
      width: 180,
      height: 80,
      x: 20,
      y: 20,
      toJSON: () => ({}),
    });
    vi.spyOn(offscreen, "getBoundingClientRect").mockReturnValue({
      top: 10,
      bottom: 200,
      left: 600,
      right: 850,
      width: 250,
      height: 190,
      x: 600,
      y: 10,
      toJSON: () => ({}),
    });
    window.history.replaceState({}, "", "/s/root");

    expect(extractVisibleChatPanelTree().regions).toEqual([
      {
        kind: "fork_pane",
        session_id: "onscreen",
        focused: false,
        messages: [{ id: "u1", role: "user", text: "Visible" }],
      },
    ]);
  });

  it("excludes messages clipped inside a fork pane scroll viewport", () => {
    document.body.innerHTML = `
      <div data-testid="chat-messages">
        <div data-testid="fork-grid">
          <div data-testid="fork-pane" data-session-id="pane">
            <div class="fork-pane-messages">
              <div data-testid="user-message" data-message-id="visible">
                <div class="message-box-body">Visible</div>
              </div>
              <div data-testid="user-message" data-message-id="clipped">
                <div class="message-box-body">Clipped</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    `;
    const chat = document.querySelector<HTMLElement>('[data-testid="chat-messages"]')!;
    const pane = document.querySelector<HTMLElement>('[data-testid="fork-pane"]')!;
    const paneMessages = document.querySelector<HTMLElement>(".fork-pane-messages")!;
    const visible = document.querySelector<HTMLElement>('[data-message-id="visible"]')!;
    const clipped = document.querySelector<HTMLElement>('[data-message-id="clipped"]')!;
    vi.spyOn(chat, "getBoundingClientRect").mockReturnValue({
      top: 0, bottom: 500, left: 0, right: 500, width: 500, height: 500, x: 0, y: 0, toJSON: () => ({}),
    });
    vi.spyOn(pane, "getBoundingClientRect").mockReturnValue({
      top: 0, bottom: 500, left: 0, right: 500, width: 500, height: 500, x: 0, y: 0, toJSON: () => ({}),
    });
    vi.spyOn(paneMessages, "getBoundingClientRect").mockReturnValue({
      top: 100, bottom: 300, left: 0, right: 500, width: 500, height: 200, x: 0, y: 100, toJSON: () => ({}),
    });
    vi.spyOn(visible, "getBoundingClientRect").mockReturnValue({
      top: 120, bottom: 180, left: 20, right: 200, width: 180, height: 60, x: 20, y: 120, toJSON: () => ({}),
    });
    vi.spyOn(clipped, "getBoundingClientRect").mockReturnValue({
      top: 360, bottom: 420, left: 20, right: 200, width: 180, height: 60, x: 20, y: 360, toJSON: () => ({}),
    });

    expect(extractVisibleChatPanelTree().regions[0]?.messages).toEqual([
      { id: "visible", role: "user", text: "Visible" },
    ]);
  });

  it("clips messages against the fork grid viewport when a pane is partially visible", () => {
    document.body.innerHTML = `
      <div data-testid="chat-messages">
        <div data-testid="fork-grid">
          <div data-testid="fork-pane" data-session-id="pane">
            <div class="fork-pane-messages">
              <div data-testid="user-message" data-message-id="visible">
                <div class="message-box-body">Visible</div>
              </div>
              <div data-testid="user-message" data-message-id="horizontal-clipped">
                <div class="message-box-body">Clipped</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    `;
    const chat = document.querySelector<HTMLElement>('[data-testid="chat-messages"]')!;
    const grid = document.querySelector<HTMLElement>('[data-testid="fork-grid"]')!;
    const pane = document.querySelector<HTMLElement>('[data-testid="fork-pane"]')!;
    const paneMessages = document.querySelector<HTMLElement>(".fork-pane-messages")!;
    const visible = document.querySelector<HTMLElement>('[data-message-id="visible"]')!;
    const clipped = document.querySelector<HTMLElement>('[data-message-id="horizontal-clipped"]')!;
    vi.spyOn(chat, "getBoundingClientRect").mockReturnValue({
      top: 0, bottom: 500, left: 0, right: 500, width: 500, height: 500, x: 0, y: 0, toJSON: () => ({}),
    });
    vi.spyOn(grid, "getBoundingClientRect").mockReturnValue({
      top: 0, bottom: 500, left: 0, right: 300, width: 300, height: 500, x: 0, y: 0, toJSON: () => ({}),
    });
    vi.spyOn(pane, "getBoundingClientRect").mockReturnValue({
      top: 0, bottom: 500, left: 200, right: 700, width: 500, height: 500, x: 200, y: 0, toJSON: () => ({}),
    });
    vi.spyOn(paneMessages, "getBoundingClientRect").mockReturnValue({
      top: 0, bottom: 500, left: 200, right: 700, width: 500, height: 500, x: 200, y: 0, toJSON: () => ({}),
    });
    vi.spyOn(visible, "getBoundingClientRect").mockReturnValue({
      top: 20, bottom: 80, left: 220, right: 280, width: 60, height: 60, x: 220, y: 20, toJSON: () => ({}),
    });
    vi.spyOn(clipped, "getBoundingClientRect").mockReturnValue({
      top: 100, bottom: 160, left: 360, right: 440, width: 80, height: 60, x: 360, y: 100, toJSON: () => ({}),
    });

    expect(extractVisibleChatPanelTree().regions[0]?.messages).toEqual([
      { id: "visible", role: "user", text: "Visible" },
    ]);
  });

  it("returns no messages when clipping ancestors leave zero visible area", () => {
    document.body.innerHTML = `
      <div data-testid="chat-messages">
        <div data-testid="fork-grid">
          <div data-testid="fork-pane" data-session-id="pane">
            <div class="fork-pane-messages">
              <div data-testid="user-message" data-message-id="hidden">
                <div class="message-box-body">Hidden</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    `;
    const chat = document.querySelector<HTMLElement>('[data-testid="chat-messages"]')!;
    const grid = document.querySelector<HTMLElement>('[data-testid="fork-grid"]')!;
    const pane = document.querySelector<HTMLElement>('[data-testid="fork-pane"]')!;
    const paneMessages = document.querySelector<HTMLElement>(".fork-pane-messages")!;
    const hidden = document.querySelector<HTMLElement>('[data-message-id="hidden"]')!;
    vi.spyOn(chat, "getBoundingClientRect").mockReturnValue({
      top: 0, bottom: 500, left: 0, right: 500, width: 500, height: 500, x: 0, y: 0, toJSON: () => ({}),
    });
    vi.spyOn(grid, "getBoundingClientRect").mockReturnValue({
      top: 0, bottom: 500, left: 0, right: 300, width: 300, height: 500, x: 0, y: 0, toJSON: () => ({}),
    });
    vi.spyOn(pane, "getBoundingClientRect").mockReturnValue({
      top: 0, bottom: 500, left: 350, right: 650, width: 300, height: 500, x: 350, y: 0, toJSON: () => ({}),
    });
    vi.spyOn(paneMessages, "getBoundingClientRect").mockReturnValue({
      top: 0, bottom: 500, left: 350, right: 650, width: 300, height: 500, x: 350, y: 0, toJSON: () => ({}),
    });
    vi.spyOn(hidden, "getBoundingClientRect").mockReturnValue({
      top: 20, bottom: 80, left: 360, right: 420, width: 60, height: 60, x: 360, y: 20, toJSON: () => ({}),
    });

    expect(extractVisibleChatPanelTree().regions[0]?.messages).toEqual([]);
  });
});
