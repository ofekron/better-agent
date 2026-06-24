// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from "vitest";
import { publishBetterAgentTestApeState } from "src/lib/testapeConsumer";
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
});
