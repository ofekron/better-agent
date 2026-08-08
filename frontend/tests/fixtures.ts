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

/** Closure 3 (ADR 0007 RuntimeProfile v2 parity): `useRuntimeProfiles.ts`
 * now cold-hydrates from `GET /api/v2/surface/runtime-profiles`, not the
 * legacy `/api/runtime-profiles` route — any fetch mock seeding a
 * `RuntimeProfilesSnapshot` fixture (e.g. via `makeRuntimeProfilesSnapshot`
 * above) for the legacy route needs the SAME data available at the v2 one
 * too, reshaped onto `RuntimeProfilesSnapshotWire`'s field names
 * (`runtime_profile_id` not `id`, `deleted_providers[].provider_id` not
 * `.id`), wrapped in the `{kind: "ok", ...}` envelope every v2 REST route
 * uses. One canonical mapping, reused by every test fixture mock instead
 * of each one re-deriving it. */
export function toRuntimeProfilesSnapshotEnvelope(snapshot: RuntimeProfilesSnapshot) {
  return {
    kind: "ok" as const,
    profiles: snapshot.runtime_profiles.map((p) => ({
      runtime_profile_id: p.id,
      provider_id: p.provider_id,
      runner: p.runner,
      default_model: p.default_model,
      default_reasoning_effort: p.default_reasoning_effort || null,
      name: p.name,
      deleted_at: p.deleted_at ?? null,
      created_at: p.created_at,
      updated_at: p.updated_at,
    })),
    default_runtime_profile_id: snapshot.default_runtime_profile_id,
    last_models: snapshot.last_models,
    last_reasoning_efforts: snapshot.last_reasoning_efforts,
    deleted_providers: snapshot.deleted_providers.map((d) => ({
      provider_id: d.id,
      name: d.name,
      nickname: d.nickname ?? "",
      kind: d.kind,
      deleted_at: d.deleted_at ?? null,
    })),
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
