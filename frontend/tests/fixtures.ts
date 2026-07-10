import type { ChatMessage, RunInfo, Session, WorkerInfo } from "../src/types";

export function makeSession(overrides: Partial<Session> = {}): Session {
  const now = new Date().toISOString();
  return {
    id: "sess-1",
    name: "test session",
    model: "claude-sonnet-4-6",
    cwd: "/tmp/proj",
    orchestration_mode: "manager",
    created_at: now,
    updated_at: now,
    messages: [],
    agent_session_id: null,
    ...overrides,
  };
}

export function makeUserMsg(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: `u-${Math.random().toString(36).slice(2, 8)}`,
    role: "user",
    content: "hello",
    events: [],
    timestamp: new Date().toISOString(),
    isStreaming: false,
    ...overrides,
  };
}

export function makeAssistantMsg(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: `a-${Math.random().toString(36).slice(2, 8)}`,
    role: "assistant",
    content: "",
    events: [],
    timestamp: new Date().toISOString(),
    isStreaming: false,
    manager: { session_id: null, events: [] },
    ...overrides,
  };
}

export function makeRun(overrides: Partial<RunInfo> = {}): RunInfo {
  const now = new Date().toISOString();
  return {
    run_id: `run-${Math.random().toString(36).slice(2, 8)}`,
    kind: "manager",
    target_message_id: null,
    started_at: now,
    last_event_at: now,
    ...overrides,
  };
}

export function makeWorker(overrides: Partial<WorkerInfo> = {}): WorkerInfo {
  return {
    agent_session_id: `worker-${Math.random().toString(36).slice(2, 6)}`,
    name: "Worker",
    orchestration_mode: "native",
    initialized: true,
    delegation_count: 0,
    ...overrides,
  };
}
