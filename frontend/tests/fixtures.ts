import type {
  ChatMessage,
  Provider,
  RunInfo,
  RuntimeProfile,
  RuntimeProfilesSnapshot,
  Session,
  WorkerInfo,
} from "../src/types";

export function makeProvider(overrides: Partial<Provider> = {}): Provider {
  return {
    id: "claude",
    generation: "generation-1",
    revision: 1,
    name: "Claude",
    kind: "claude",
    mode: "subscription",
    base_url: "",
    config_dir: "",
    custom_models: [],
    runner_options: ["native"],
    runner_profiles: [
      { runner: "native", reasoning_efforts: ["low", "medium", "high", "xhigh"] },
    ],
    suspended: false,
    reasoning_effort_options: ["low", "medium", "high", "xhigh"],
    permission_options: {},
    default_permission: {},
    has_api_key: false,
    supports_fork: true,
    supports_manager_mode: true,
    supports_rewind: true,
    supports_steering: true,
    supports_native_subagents: false,
    supports_reasoning_effort: true,
    capability_overrides: {},
    ...overrides,
  };
}

export function makeRuntimeProfile(
  overrides: Partial<RuntimeProfile> = {},
): RuntimeProfile {
  const now = "2026-01-01T00:00:00Z";
  return {
    id: "rp-1",
    provider_id: "claude",
    runner: "native",
    name: "Claude",
    default_model: "sonnet",
    default_reasoning_effort: "medium",
    created_at: now,
    updated_at: now,
    deleted_at: null,
    ...overrides,
  };
}

export function makeRuntimeProfilesSnapshot(
  overrides: Partial<RuntimeProfilesSnapshot> = {},
): RuntimeProfilesSnapshot {
  return {
    runtime_profiles: [makeRuntimeProfile()],
    default_runtime_profile_id: "rp-1",
    deleted_providers: [],
    last_models: {},
    last_reasoning_efforts: {},
    ...overrides,
  };
}

export function makeSession(overrides: Partial<Session> = {}): Session {
  const now = new Date().toISOString();
  return {
    id: "sess-1",
    name: "test session",
    model: "claude-sonnet-4-6",
    cwd: "/tmp/proj",
    orchestration_mode: "native",
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

export function makeOperatorMsg(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: `o-${Math.random().toString(36).slice(2, 8)}`,
    role: "operator",
    content: "",
    events: [],
    timestamp: new Date().toISOString(),
    isStreaming: false,
    source: "operator",
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
