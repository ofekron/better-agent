import type { Session } from "src/types";

type TestApeClient = {
  sendState(key: string, value: unknown): void;
};

type UpdateInput = {
  authStatus: string;
  connected: boolean;
  viewport: string;
  sessions: readonly Session[];
  currentSession: Session | null;
  openSessionIds: readonly string[];
  pendingMessageCount: number;
  queuedPromptCount: number;
  rightPanelOpen: boolean;
  rightPanelTab: string | null;
};

declare global {
  interface Window {
    testape?: TestApeClient;
  }
}

function client(): TestApeClient | null {
  return typeof window !== "undefined" && window.testape ? window.testape : null;
}

function sessionPayload(session: Session | null): Record<string, unknown> | null {
  if (!session) return null;
  return {
    id: session.id,
    name: session.name || "",
    cwd: session.cwd || "",
    provider_id: session.provider_id || "",
    model: session.model || "",
    orchestration_mode: session.orchestration_mode || "",
    message_count: session.messages?.length ?? 0,
  };
}

function sendState(testape: TestApeClient, key: string, value: unknown): void {
  testape.sendState(`better_agent_${key}`, value);
}

export function publishBetterAgentTestApeState(input: UpdateInput): void {
  const testape = client();
  if (!testape) return;
  const currentSession = sessionPayload(input.currentSession);
  const openFilePanels = input.currentSession?.open_file_panels ?? [];

  sendState(testape, "auth_status", input.authStatus);
  sendState(testape, "backend_connected", input.connected);
  sendState(testape, "viewport", input.viewport);
  sendState(testape, "session_count", input.sessions.length);
  sendState(testape, "current_session", currentSession);
  sendState(testape, "current_session_id", input.currentSession?.id ?? null);
  sendState(testape, "open_session_ids", [...input.openSessionIds]);
  sendState(testape, "pending_message_count", input.pendingMessageCount);
  sendState(testape, "queued_prompt_count", input.queuedPromptCount);
  sendState(testape, "right_panel", {
    open: input.rightPanelOpen,
    tab: input.rightPanelTab,
  });
  sendState(testape, "native_file_panel_count", openFilePanels.length);
  sendState(testape, "native_file_panel_paths", openFilePanels.map((panel) => panel.path));
}
