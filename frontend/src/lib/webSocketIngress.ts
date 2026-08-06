import {
  SESSION_STATUS_KEYS,
  type WireEvent,
  type WSEventType,
} from "../types";

type WireEventParseErrorCode =
  | "invalid_envelope"
  | "untrusted_session"
  | "invalid_core_payload";

type WireEventParseResult =
  | { ok: true; event: WireEvent }
  | {
      ok: false;
      error: {
        code: WireEventParseErrorCode;
        event_type?: string;
      };
    };

type PayloadValidator = (data: Record<string, unknown>) => boolean;

function hasControlCharacter(value: string): boolean {
  return Array.from(value).some((character) => {
    const code = character.charCodeAt(0);
    return code <= 31 || code === 127;
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isTrustedSessionId(value: unknown): value is string {
  return typeof value === "string"
    && value.length > 0
    && value.length <= 512
    && value.trim() === value
    && !hasControlCharacter(value);
}

function isEventType(value: unknown): value is string {
  return typeof value === "string"
    && value.length > 0
    && value.length <= 512
    && !hasControlCharacter(value);
}

function hasString(data: Record<string, unknown>, key: string): boolean {
  const value = data[key];
  return typeof value === "string" && value.length > 0;
}

function hasOwn(data: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(data, key);
}

function hasRecord(data: Record<string, unknown>, key: string): boolean {
  return isRecord(data[key]);
}

function hasArray(data: Record<string, unknown>, key: string): boolean {
  return Array.isArray(data[key]);
}

function hasBoolean(data: Record<string, unknown>, key: string): boolean {
  return typeof data[key] === "boolean";
}

function hasOneString(data: Record<string, unknown>, keys: readonly string[]): boolean {
  return keys.some((key) => hasString(data, key));
}

function hasStringArray(data: Record<string, unknown>, key: string): boolean {
  const value = data[key];
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function hasNonNegativeInteger(data: Record<string, unknown>, key: string): boolean {
  const value = data[key];
  return Number.isSafeInteger(value) && Number(value) >= 0;
}

function hasRecordWithString(
  data: Record<string, unknown>,
  key: string,
  nestedKey: string,
): boolean {
  const value = data[key];
  return isRecord(value) && hasString(value, nestedKey);
}

function messagesPayload(data: Record<string, unknown>): boolean {
  return hasString(data, "app_session_id") && hasArray(data, "messages");
}

function isNullableString(value: unknown): boolean {
  return value === null || typeof value === "string";
}

function isOptionalNullableString(value: unknown): boolean {
  return value === undefined || isNullableString(value);
}

function noFieldPayload(): boolean {
  return true;
}

function appSessionPayload(data: Record<string, unknown>): boolean {
  return hasString(data, "app_session_id");
}

function sessionMessagePayload(data: Record<string, unknown>): boolean {
  return hasString(data, "session_id") && hasString(data, "msg_id");
}

function sessionOwnedPayload(data: Record<string, unknown>): boolean {
  return hasOneString(data, ["app_session_id", "session_id", "manager_session_id"]);
}

function userMessageLifecyclePayload(data: Record<string, unknown>): boolean {
  return hasString(data, "app_session_id") && hasString(data, "lifecycle_msg_id");
}

function tokenUsagePayload(value: unknown): boolean {
  return isRecord(value) && Object.values(value).every(
    (count) => Number.isSafeInteger(count) && Number(count) >= 0,
  );
}

function turnCompletePayload(data: Record<string, unknown>): boolean {
  if (hasOwn(data, "success")) {
    return hasBoolean(data, "success")
      && !hasOwn(data, "workers_used")
      && !hasOwn(data, "total_token_usage")
      && !hasOwn(data, "trace_id")
      && (data.app_session_id === undefined || hasString(data, "app_session_id"))
      && isOptionalNullableString(data.session_id)
      && (data.token_usage === undefined
        || data.token_usage === null
        || tokenUsagePayload(data.token_usage));
  }

  return !hasOwn(data, "session_id")
    && !hasOwn(data, "token_usage")
    && hasString(data, "app_session_id")
    && hasStringArray(data, "workers_used")
    && tokenUsagePayload(data.total_token_usage)
    && hasString(data, "trace_id");
}

function providerInstallProgressPayload(data: Record<string, unknown>): boolean {
  if (!hasString(data, "kind")) return false;
  if (data.phase === "started") return true;
  return (data.stream === "stdout" || data.stream === "stderr")
    && typeof data.text === "string";
}

function providerInstallFinishedPayload(data: Record<string, unknown>): boolean {
  const lines = data.lines;
  return hasString(data, "kind")
    && hasString(data, "label")
    && hasString(data, "command")
    && (data.state === "running" || data.state === "succeeded" || data.state === "failed")
    && Array.isArray(lines)
    && lines.every((line) => isRecord(line)
      && (line.s === "stdout" || line.s === "stderr")
      && typeof line.t === "string")
    && isNullableString(data.started_at)
    && isNullableString(data.finished_at)
    && (data.returncode === null || Number.isSafeInteger(data.returncode))
    && (data.installed === null || typeof data.installed === "boolean")
    && isNullableString(data.message);
}

const BACKGROUND_WORK_STATUSES = new Set([
  "pending", "running", "succeeded", "failed", "cancelled", "unknown",
]);

function backgroundWorkProgressValid(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (!isRecord(value)) return false;
  const total = value.total;
  return typeof value.completed === "number"
    && (total === null || typeof total === "number")
    && typeof value.unit === "string";
}

function backgroundWorkChangedPayload(data: Record<string, unknown>): boolean {
  if (!hasString(data, "epoch")) return false;
  if (data.cleared === true) return true;
  if (typeof data.removed_id === "string") return typeof data.seq === "number";
  const item = data.item;
  return isRecord(item)
    && hasString(item, "id")
    && hasString(item, "owner_kind")
    && hasString(item, "owner_id")
    && hasString(item, "label")
    && typeof item.status === "string"
    && BACKGROUND_WORK_STATUSES.has(item.status)
    && typeof item.seq === "number"
    && hasString(item, "started_at")
    && hasString(item, "updated_at")
    && isNullableString(item.detail)
    && isNullableString(item.phase)
    && isNullableString(item.error)
    && isNullableString(item.finished_at)
    && isNullableString(item.session_id)
    && backgroundWorkProgressValid(item.progress)
    && Array.isArray(item.actions);
}

const SESSION_STATUS_KEY_SET = new Set<string>(SESSION_STATUS_KEYS);

function projectAggregateRecord(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return hasString(value, "path")
    && hasString(value, "node_id")
    && hasNonNegativeInteger(value, "running_count")
    && hasNonNegativeInteger(value, "unread_session_count")
    && hasNonNegativeInteger(value, "waiting_for_user_count")
    && hasNonNegativeInteger(value, "errored_count");
}

function projectAggregatesChangedPayload(data: Record<string, unknown>): boolean {
  return hasString(data, "epoch")
    && hasNonNegativeInteger(data, "revision")
    && Array.isArray(data.upserts)
    && data.upserts.every(projectAggregateRecord)
    && Array.isArray(data.tombstones)
    && data.tombstones.every(projectAggregateRecord);
}

function nodeStateChangedPayload(data: Record<string, unknown>): boolean {
  return hasString(data, "node_id")
    && (data.state === "connected" || data.state === "disconnected" || data.state === "unknown")
    && (data.last_seen === null || typeof data.last_seen === "number");
}

const NODE_PROVIDER_CREDENTIAL_STATUS_KEYS = new Set([
  "node_id",
  "provider_id",
  "provider_name",
  "status",
  "authorized_at",
  "updated_at",
  "failure_code",
]);

function nodeProviderCredentialsChangedPayload(
  data: Record<string, unknown>,
): boolean {
  return Object.keys(data).length === 2
    && hasOwn(data, "node_id")
    && hasOwn(data, "provider_credentials")
    && hasString(data, "node_id")
    && Array.isArray(data.provider_credentials)
    && data.provider_credentials.every((value) => {
      if (!isRecord(value)) return false;
      return Object.keys(value).every((key) =>
        NODE_PROVIDER_CREDENTIAL_STATUS_KEYS.has(key)
      )
        && hasString(value, "node_id")
        && hasString(value, "provider_id")
        && hasString(value, "provider_name")
        && (
          value.status === "pending"
          || value.status === "synced"
          || value.status === "failed"
        )
        && hasString(value, "authorized_at")
        && hasString(value, "updated_at")
        && (
          !hasOwn(value, "failure_code")
          || typeof value.failure_code === "string"
        );
    });
}

function nodeRegistrationRequestedPayload(data: Record<string, unknown>): boolean {
  return hasString(data, "node_id")
    && hasString(data, "address")
    && hasStringArray(data, "cwd_roots")
    && hasString(data, "fingerprint")
    && (data.status === "pending" || data.status === "approved" || data.status === "denied")
    && hasString(data, "created_at")
    && hasString(data, "expires_at");
}

function userInputRequestedPayload(data: Record<string, unknown>): boolean {
  return hasString(data, "request_id")
    && hasString(data, "app_session_id")
    && data.status === "pending"
    && (data.kind === "input" || data.kind === "approval" || data.kind === "memory");
}

function toolApprovalRequestedPayload(data: Record<string, unknown>): boolean {
  return hasString(data, "approval_id")
    && hasString(data, "app_session_id")
    && hasString(data, "run_id")
    && hasString(data, "provider_kind")
    && hasString(data, "tool_name")
    && hasRecord(data, "summary");
}

function workerCreationRequestedPayload(data: Record<string, unknown>): boolean {
  return hasString(data, "delegation_id") && hasString(data, "app_session_id");
}

function uiSelectionChangedPayload(data: Record<string, unknown>): boolean {
  const selected = data.selected_project;
  return (selected === null
      || (isRecord(selected) && hasString(selected, "path") && hasString(selected, "node_id")))
    && isRecord(data.remembered_session_by_project)
    && (data.open_session_tab_ids === undefined || hasStringArray(data, "open_session_tab_ids"))
    && (data.open_session_tab_joined_at === undefined
      || isRecord(data.open_session_tab_joined_at));
}

const UNPROJECTED_WIRE_EVENT_TYPES = [
  // Provider-native or historical render payloads stay opaque.
  "agent_message",
  "thinking",
  "tool_call",
  "output",
  "steer_prompt",
  "session_discovered",
  "complete",
  "manager_event",
  "model_fallback",
  // Snapshot framing and heartbeat are consumed by the transport itself.
  "snapshot_begin",
  "snapshot_chunk",
  "snapshot_end",
  "snapshot_restart_required",
  "snapshot_refresh_required",
  "snapshot_refresh_complete",
  "snapshot_cancelled",
  "pong",
  // Namespaced extension payloads are owned by their extensions.
  "extension.catalog",
  "extension.config",
  "extension.config.settings",
  "extension.config.instructions",
  "extension.config.ui_settings",
  "extension.config.internal_llm",
  "extension.config.permissions",
  "extension.config.mcp",
  "extension.config.skills",
  "extension.ui",
  "extension.ui.frontend_modules",
  "extension.harness.default",
  "extension.harness.default.disabled_builtin_extensions",
  "extension.harness.default.disabled_builtin_tools",
  // These names never mutate a current prompt/session/global projector.
  "turn_started",
  "diagnostic",
  "lifecycle_notice",
  "tool_result",
  "pr_link",
  "command_received",
] as const satisfies readonly WSEventType[];

type ValidatedCoreEventType = Exclude<
  WSEventType,
  (typeof UNPROJECTED_WIRE_EVENT_TYPES)[number]
>;

/** Core events consumed by prompt, session, or global state projectors validate
 * before publication. The explicit complement above keeps this registry
 * compile-time exhaustive without claiming opaque payload ownership. */
export const knownCoreEventValidators: Readonly<
  Record<ValidatedCoreEventType, PayloadValidator>
> = Object.freeze({
  messages_replay: messagesPayload,
  messages_delta: messagesPayload,
  user_message_persisted: (data) =>
    hasString(data, "session_id") && hasRecord(data, "user_message"),
  steer_prompt_persisted: appSessionPayload,
  user_message_queued: userMessageLifecyclePayload,
  user_message_sent: userMessageLifecyclePayload,
  user_message_received: userMessageLifecyclePayload,
  user_message_done: userMessageLifecyclePayload,
  user_message_failed: userMessageLifecyclePayload,
  run_state: (data) =>
    hasString(data, "app_session_id") && hasArray(data, "runs"),
  session_reconciled: (data) => hasString(data, "root_id"),
  turn_start: sessionOwnedPayload,
  turn_complete: turnCompletePayload,
  turn_stopped: (data) =>
    hasString(data, "app_session_id")
    && (data.stopped_at === undefined || hasString(data, "stopped_at"))
    && isOptionalNullableString(data.interrupted_by_msg_id),
  turn_detached: appSessionPayload,
  worker_start: appSessionPayload,
  worker_event: appSessionPayload,
  worker_complete: appSessionPayload,
  worker_prep_start: appSessionPayload,
  worker_prep_event: appSessionPayload,
  worker_prep_complete: appSessionPayload,
  worker_prep_cancelled: appSessionPayload,
  model_switched: appSessionPayload,
  todos_snapshot: (data) =>
    hasOneString(data, ["app_session_id", "session_id"])
    && (hasArray(data, "todos") || hasArray(data, "items")),
  error: (data) => typeof data.error === "string",
  rewind_complete: (data) =>
    hasString(data, "session_id") && hasArray(data, "messages"),
  message_recovering_changed: (data) =>
    sessionMessagePayload(data) && hasBoolean(data, "value"),
  message_retrying_changed: (data) =>
    sessionMessagePayload(data)
    && hasOwn(data, "retry_at")
    && isOptionalNullableString(data.retry_at)
    && isOptionalNullableString(data.error_text),
  message_error_changed: (data) =>
    sessionMessagePayload(data)
    && hasOwn(data, "error_text")
    && isNullableString(data.error_text),
  message_auto_retry_changed: (data) =>
    sessionMessagePayload(data)
    && hasOwn(data, "auto_retry")
    && (data.auto_retry === null
      || (isRecord(data.auto_retry)
        && hasNonNegativeInteger(data.auto_retry, "count")
        && hasString(data.auto_retry, "kind"))),
  message_content_updated: (data) =>
    sessionMessagePayload(data) && typeof data.content === "string",
  message_continuation_changed: (data) =>
    sessionMessagePayload(data)
    && hasOwn(data, "chain_depth")
    && (data.chain_depth === null || hasNonNegativeInteger(data, "chain_depth")),
  message_run_meta_changed: (data) =>
    sessionMessagePayload(data)
    && hasOwn(data, "run_meta")
    && (data.run_meta === null || isRecord(data.run_meta)),
  message_ask_result_changed: (data) =>
    sessionMessagePayload(data)
    && hasOwn(data, "ask_result")
    && (data.ask_result === null
      || (isRecord(data.ask_result)
        && hasArray(data.ask_result, "results")
        && typeof data.ask_result.reasoning === "string")),
  message_ask_choice_changed: (data) =>
    sessionMessagePayload(data)
    && hasOwn(data, "chosen_session_id")
    && isNullableString(data.chosen_session_id),
  session_metadata_updated: (data) =>
    hasString(data, "session_id") && hasRecord(data, "patch"),
  session_created: (data) => hasRecordWithString(data, "session", "id"),
  session_forked: (data) => {
    if (!hasRecordWithString(data, "session", "id")) return false;
    return data.parent_session_id === null || hasString(data, "parent_session_id");
  },
  session_deleted: (data) => hasString(data, "session_id"),
  session_renamed: (data) =>
    hasString(data, "session_id") && hasString(data, "name"),
  project_updates_changed: (data) =>
    hasString(data, "project_id")
    && hasNonNegativeInteger(data, "unseen_count"),
  project_aggregates_changed: projectAggregatesChangedPayload,
  session_status_changed: (data) =>
    hasString(data, "session_id")
    && typeof data.status_key === "string"
    && SESSION_STATUS_KEY_SET.has(data.status_key)
    && hasNonNegativeInteger(data, "status_rank"),
  prompt_queued: (data) =>
    hasString(data, "app_session_id")
    && hasString(data, "queued_id")
    && typeof data.prompt_preview === "string"
    && (data.send_mode === undefined || hasString(data, "send_mode"))
    && hasNonNegativeInteger(data, "queue_position"),
  queue_consumed: (data) =>
    hasString(data, "app_session_id")
    && (data.queued_id === null || hasString(data, "queued_id")),
  session_running_changed: (data) =>
    hasString(data, "session_id") && typeof data.value === "boolean",
  session_unread_changed: (data) =>
    hasString(data, "session_id") && hasNonNegativeInteger(data, "unread_count"),
  session_error_changed: (data) =>
    hasString(data, "session_id") && typeof data.has_error === "boolean",
  session_user_input_changed: (data) =>
    hasString(data, "session_id")
    && hasNonNegativeInteger(data, "pending_user_input_count"),
  session_marker_changed: (data) =>
    hasString(data, "session_id")
    && hasString(data, "extension_id")
    && (data.marker === null || isRecord(data.marker)),
  session_monitoring_changed: (data) =>
    hasString(data, "session_id") && hasString(data, "monitoring_state"),
  session_provenance_changed: (data) => hasString(data, "session_id"),
  supervisor_event: (data) => hasString(data, "kind"),
  projects_changed: noFieldPayload,
  workers_changed: noFieldPayload,
  session_organization_changed: noFieldPayload,
  project_mappings_changed: noFieldPayload,
  user_prefs_changed: noFieldPayload,
  ui_selection_changed: uiSelectionChangedPayload,
  credential_consent_changed: noFieldPayload,
  node_state_changed: nodeStateChangedPayload,
  node_provider_credentials_changed: nodeProviderCredentialsChangedPayload,
  node_registration_requested: nodeRegistrationRequestedPayload,
  node_registration_resolved: (data) =>
    hasString(data, "node_id")
    && (data.status === "approved" || data.status === "denied"),
  user_input_requested: userInputRequestedPayload,
  user_input_resolved: (data) => hasString(data, "request_id"),
  tool_approval_requested: toolApprovalRequestedPayload,
  tool_approval_resolved: (data) => hasString(data, "approval_id"),
  worker_creation_requested: workerCreationRequestedPayload,
  worker_creation_approved: (data) => hasString(data, "delegation_id"),
  worker_creation_failed: (data) => hasString(data, "delegation_id"),
  provider_changed: noFieldPayload,
  installation_capabilities_changed: noFieldPayload,
  provider_install_progress: providerInstallProgressPayload,
  provider_install_finished: providerInstallFinishedPayload,
  models_catalog_changed: (data) => hasString(data, "provider_id"),
  background_work_changed: backgroundWorkChangedPayload,
  schedules_updated: (data) =>
    hasString(data, "app_session_id") && hasArray(data, "schedules"),
  schedules_changed: noFieldPayload,
  extension_updates_changed: (data) => hasStringArray(data, "available"),
  extension_event: (data) =>
    hasString(data, "extension_id")
    && hasString(data, "event_name")
    && hasRecord(data, "data"),
} satisfies Record<ValidatedCoreEventType, PayloadValidator>);

function failure(
  code: WireEventParseErrorCode,
  eventType?: string,
): WireEventParseResult {
  return {
    ok: false,
    error: eventType ? { code, event_type: eventType } : { code },
  };
}

function parseWireEventUnsafe(value: unknown): WireEventParseResult {
  if (!isRecord(value)) return failure("invalid_envelope");

  const type = value.type;
  if (!isEventType(type)) return failure("invalid_envelope");
  let data = value.data;
  if (
    !isRecord(data)
    && type === "rewind_complete"
    && hasString(value, "session_id")
    && hasArray(value, "messages")
  ) {
    data = { session_id: value.session_id, messages: value.messages };
  }
  if (!isRecord(data)) return failure("invalid_envelope", type);

  const seq = value.seq;
  if (seq !== undefined && (!Number.isSafeInteger(seq) || Number(seq) < 0)) {
    return failure("invalid_envelope", type);
  }

  const sessionId = value.session_id;
  if (sessionId !== undefined && !isTrustedSessionId(sessionId)) {
    return failure("invalid_envelope", type);
  }
  if (seq !== undefined && sessionId === undefined) {
    return failure("untrusted_session", type);
  }

  const validator = Object.prototype.hasOwnProperty.call(knownCoreEventValidators, type)
    && !(UNPROJECTED_WIRE_EVENT_TYPES as readonly string[]).includes(type)
      ? knownCoreEventValidators[type as ValidatedCoreEventType]
      : undefined;
  if (validator && !validator(data)) {
    return failure("invalid_core_payload", type);
  }

  const event: WireEvent = { type, data };
  if (seq !== undefined) event.seq = Number(seq);
  if (sessionId !== undefined) event.session_id = sessionId;
  return { ok: true, event };
}

/** Parse a decoded WebSocket frame without ever throwing. */
export function parseWireEvent(value: unknown): WireEventParseResult {
  try {
    return parseWireEventUnsafe(value);
  } catch {
    return failure("invalid_envelope");
  }
}
