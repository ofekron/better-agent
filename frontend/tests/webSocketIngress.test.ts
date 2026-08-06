import { describe, expect, it } from "vitest";

import {
  knownCoreEventValidators,
  parseWireEvent,
} from "../src/lib/webSocketIngress";

const BASE_CORE_PAYLOAD_CASES = [
  {
    type: "messages_replay",
    valid: { app_session_id: "session-a", messages: [] },
    invalid: { app_session_id: "session-a", messages: "not-an-array" },
  },
  {
    type: "messages_delta",
    valid: { app_session_id: "session-a", messages: [{}] },
    invalid: { app_session_id: 7, messages: [] },
  },
  {
    type: "user_message_persisted",
    valid: { session_id: "session-a", user_message: { id: "message-a" } },
    invalid: { session_id: "session-a", user_message: null },
  },
  {
    type: "run_state",
    valid: { app_session_id: "session-a", runs: [] },
    invalid: { app_session_id: "session-a", runs: {} },
  },
  {
    type: "session_metadata_updated",
    valid: { session_id: "session-a", patch: {} },
    invalid: { session_id: "session-a", patch: [] },
  },
  {
    type: "session_created",
    valid: { session: { id: "session-a" } },
    invalid: { session: {} },
  },
  {
    type: "session_forked",
    valid: { session: { id: "fork-a" }, parent_session_id: null },
    invalid: { session: { id: "fork-a" }, parent_session_id: 7 },
  },
  {
    type: "session_deleted",
    valid: { session_id: "session-a" },
    invalid: { session_id: "" },
  },
  {
    type: "session_renamed",
    valid: { session_id: "session-a", name: "Renamed" },
    invalid: { session_id: "session-a", name: 7 },
  },
  {
    type: "project_updates_changed",
    valid: { project_id: "project-a", unseen_count: 0 },
    invalid: { project_id: "project-a", unseen_count: -1 },
  },
  {
    type: "session_status_changed",
    valid: { session_id: "session-a", status_key: "running", status_rank: 2 },
    invalid: { session_id: "session-a", status_key: "invented", status_rank: 2 },
  },
  {
    type: "project_aggregates_changed",
    valid: {
      epoch: "epoch-a",
      revision: 2,
      upserts: [{
        path: "/repo",
        node_id: "primary",
        running_count: 1,
        unread_session_count: 0,
        waiting_for_user_count: 0,
        errored_count: 0,
      }],
      tombstones: [],
    },
    invalid: {
      epoch: "epoch-a",
      revision: 2,
      upserts: [{ path: "/repo", node_id: "primary", running_count: -1 }],
      tombstones: [],
    },
  },
  {
    type: "prompt_queued",
    valid: {
      app_session_id: "session-a",
      queued_id: "queued-a",
      prompt_preview: "retry",
      queue_position: 0,
    },
    invalid: {
      app_session_id: "session-a",
      queued_id: "queued-a",
      prompt_preview: "retry",
      queue_position: -1,
    },
  },
  {
    type: "queue_consumed",
    valid: { app_session_id: "session-a", queued_id: null },
    invalid: { app_session_id: "session-a", queued_id: 7 },
  },
  {
    type: "session_running_changed",
    valid: { session_id: "session-a", value: false },
    invalid: { session_id: "session-a", value: 0 },
  },
  {
    type: "session_unread_changed",
    valid: { session_id: "session-a", unread_count: 0 },
    invalid: { session_id: "session-a", unread_count: -1 },
  },
  {
    type: "session_error_changed",
    valid: { session_id: "session-a", has_error: false },
    invalid: { session_id: "session-a", has_error: "false" },
  },
  {
    type: "session_user_input_changed",
    valid: { session_id: "session-a", pending_user_input_count: 0 },
    invalid: { session_id: "session-a", pending_user_input_count: -1 },
  },
  {
    type: "session_marker_changed",
    valid: { session_id: "session-a", extension_id: "extension-a", marker: null },
    invalid: { session_id: "session-a", extension_id: "extension-a", marker: [] },
  },
  {
    type: "session_monitoring_changed",
    valid: { session_id: "session-a", monitoring_state: "stopped" },
    invalid: { session_id: "session-a", monitoring_state: null },
  },
  {
    type: "provider_install_progress",
    valid: { kind: "codex", stream: "stdout", text: "installed" },
    invalid: { kind: "codex", stream: "log", text: "installed" },
  },
  {
    type: "provider_install_finished",
    valid: {
      kind: "codex",
      label: "Codex",
      command: "npm install",
      state: "succeeded",
      lines: [{ s: "stdout", t: "installed" }],
      started_at: "2026-08-04T10:00:00Z",
      finished_at: "2026-08-04T10:01:00Z",
      returncode: 0,
      installed: true,
      message: null,
    },
    invalid: {
      kind: "codex",
      label: "Codex",
      command: "npm install",
      state: "succeeded",
      lines: [{ s: "log", t: "installed" }],
      started_at: "2026-08-04T10:00:00Z",
      finished_at: "2026-08-04T10:01:00Z",
      returncode: 0,
      installed: true,
      message: null,
    },
  },
  {
    type: "models_catalog_changed",
    valid: { provider_id: "codex", projection: null },
    invalid: { provider_id: "" },
  },
  {
    type: "background_work_changed",
    valid: {
      epoch: "epoch-1",
      item: {
        id: "core:startup:recover",
        kind: "work",
        owner_kind: "core",
        owner_id: "startup",
        label: "startup_tasks.recover_in_flight",
        status: "running",
        seq: 1,
        started_at: "2026-08-04T10:00:00Z",
        updated_at: "2026-08-04T10:00:00Z",
        session_id: null,
        detail: null,
        phase: null,
        error: null,
        finished_at: null,
        progress: null,
        actions: [],
      },
    },
    invalid: { epoch: "epoch-1", item: { id: "core:startup:recover", status: "exploded" } },
  },
  {
    type: "extension_event",
    valid: {
      extension_id: "extension-a",
      event_name: "extension_toast",
      data: { message: "Ready", level: "success" },
    },
    invalid: {
      extension_id: "extension-a",
      event_name: "extension_toast",
      data: "not-an-object",
    },
  },
] as const;

const APP_SESSION_CORE_TYPES = [
  "steer_prompt_persisted",
  "turn_detached",
  "worker_start",
  "worker_event",
  "worker_complete",
  "worker_prep_start",
  "worker_prep_event",
  "worker_prep_complete",
  "worker_prep_cancelled",
  "model_switched",
] as const;

const LIFECYCLE_CORE_TYPES = [
  "user_message_queued",
  "user_message_sent",
  "user_message_received",
  "user_message_done",
  "user_message_failed",
] as const;

const MESSAGE_CHANGE_CORE_TYPES = [
  "message_recovering_changed",
  "message_retrying_changed",
  "message_error_changed",
  "message_auto_retry_changed",
  "message_content_updated",
  "message_continuation_changed",
  "message_run_meta_changed",
  "message_ask_result_changed",
  "message_ask_choice_changed",
] as const;

const NO_FIELD_CORE_TYPES = [
  "projects_changed",
  "workers_changed",
  "session_organization_changed",
  "project_mappings_changed",
  "user_prefs_changed",
  "credential_consent_changed",
  "provider_changed",
  "installation_capabilities_changed",
  "schedules_changed",
] as const;

const VALID_MESSAGE_CHANGE_PAYLOAD = {
  session_id: "session-a",
  msg_id: "message-a",
  value: false,
  retry_at: null,
  error_text: null,
  auto_retry: null,
  content: "",
  chain_depth: null,
  run_meta: null,
  ask_result: null,
  chosen_session_id: null,
};

const CORE_PAYLOAD_CASES = [
  ...BASE_CORE_PAYLOAD_CASES,
  ...APP_SESSION_CORE_TYPES.map((type) => ({
    type,
    valid: { app_session_id: "session-a" },
    invalid: {},
  })),
  ...LIFECYCLE_CORE_TYPES.map((type) => ({
    type,
    valid: { app_session_id: "session-a", lifecycle_msg_id: "message-a" },
    invalid: { app_session_id: "session-a" },
  })),
  ...MESSAGE_CHANGE_CORE_TYPES.map((type) => ({
    type,
    valid: VALID_MESSAGE_CHANGE_PAYLOAD,
    invalid: {},
  })),
  ...NO_FIELD_CORE_TYPES.map((type) => ({
    type,
    valid: {},
    invalid: undefined,
  })),
  {
    type: "session_reconciled",
    valid: { root_id: "session-a" },
    invalid: {},
  },
  {
    type: "turn_start",
    valid: { manager_session_id: "manager-a" },
    invalid: {},
  },
  {
    type: "turn_complete",
    valid: { success: true },
    invalid: {},
  },
  {
    type: "turn_stopped",
    valid: { app_session_id: "session-a", stopped_at: "2026-08-04T10:00:00Z" },
    invalid: {},
  },
  {
    type: "todos_snapshot",
    valid: { session_id: "session-a", todos: [] },
    invalid: { session_id: "session-a" },
  },
  {
    type: "error",
    valid: { error: "failed" },
    invalid: {},
  },
  {
    type: "rewind_complete",
    valid: { session_id: "session-a", messages: [] },
    invalid: { session_id: "session-a" },
  },
  {
    type: "session_provenance_changed",
    valid: { session_id: "session-a" },
    invalid: {},
  },
  {
    type: "supervisor_event",
    valid: { kind: "await_user" },
    invalid: {},
  },
  {
    type: "ui_selection_changed",
    valid: {
      selected_project: null,
      remembered_session_by_project: {},
      open_session_tab_ids: [],
    },
    invalid: { selected_project: null },
  },
  {
    type: "node_state_changed",
    valid: { node_id: "node-a", state: "connected", last_seen: null },
    invalid: { node_id: "node-a", state: "invented", last_seen: null },
  },
  {
    type: "node_registration_requested",
    valid: {
      node_id: "node-a",
      address: "node.local",
      cwd_roots: ["/repo"],
      fingerprint: "abc123",
      status: "pending",
      created_at: "2026-08-04T10:00:00Z",
      expires_at: "2026-08-04T10:05:00Z",
    },
    invalid: { node_id: "node-a" },
  },
  {
    type: "node_registration_resolved",
    valid: { node_id: "node-a", status: "approved" },
    invalid: { node_id: "node-a", status: "pending" },
  },
  {
    type: "node_provider_credentials_changed",
    valid: {
      node_id: "node-a",
      provider_credentials: [
        {
          node_id: "node-a",
          provider_id: "claude",
          provider_name: "Claude",
          status: "synced",
          authorized_at: "2026-08-04T10:00:00Z",
          updated_at: "2026-08-04T10:00:00Z",
        },
      ],
    },
    invalid: { node_id: "node-a" },
  },
  {
    type: "user_input_requested",
    valid: {
      request_id: "request-a",
      app_session_id: "session-a",
      status: "pending",
      kind: "input",
    },
    invalid: { request_id: "request-a", status: "pending", kind: "input" },
  },
  {
    type: "user_input_resolved",
    valid: { request_id: "request-a" },
    invalid: {},
  },
  {
    type: "tool_approval_requested",
    valid: {
      approval_id: "approval-a",
      app_session_id: "session-a",
      run_id: "run-a",
      provider_kind: "codex",
      tool_name: "exec",
      summary: {},
    },
    invalid: { approval_id: "approval-a" },
  },
  {
    type: "tool_approval_resolved",
    valid: { approval_id: "approval-a" },
    invalid: {},
  },
  {
    type: "worker_creation_requested",
    valid: { delegation_id: "delegation-a", app_session_id: "session-a" },
    invalid: { delegation_id: "delegation-a" },
  },
  {
    type: "worker_creation_approved",
    valid: { delegation_id: "delegation-a" },
    invalid: {},
  },
  {
    type: "worker_creation_failed",
    valid: { delegation_id: "delegation-a", error: "failed" },
    invalid: {},
  },
  {
    type: "schedules_updated",
    valid: { app_session_id: "session-a", schedules: [] },
    invalid: { app_session_id: "session-a" },
  },
  {
    type: "extension_updates_changed",
    valid: { available: ["extension-a"] },
    invalid: { available: [7] },
  },
] as const;

describe("parseWireEvent", () => {
  it("parses a sequenced core event with trusted routing metadata", () => {
    const data = { app_session_id: "session-a", messages: [] };

    expect(parseWireEvent({
      type: "messages_replay",
      data,
      seq: 7,
      session_id: "session-a",
      ignored_outer_field: "not part of the trusted envelope",
    })).toEqual({
      ok: true,
      event: {
        type: "messages_replay",
        data,
        seq: 7,
        session_id: "session-a",
      },
    });
  });

  it("accepts the initial nonnegative sequence cursor", () => {
    expect(parseWireEvent({
      type: "messages_delta",
      data: { app_session_id: "session-a", messages: [] },
      seq: 0,
      session_id: "session-a",
    })).toMatchObject({ ok: true, event: { seq: 0 } });
  });

  it.each([
    ["unknown", "future_event", { any: ["shape"] }],
    ["provider-native", "agent_message", { type: 42, provider_field: true }],
    ["legacy", "manager_event", { old: "shape" }],
    ["namespaced", "extension.todos.changed", { version: "future" }],
    ["future grammar", "provider/future event", { slash: true }],
    ["prototype collision", "hasOwnProperty", { inherited: false }],
  ])("keeps %s payloads opaque", (_kind, type, data) => {
    const result = parseWireEvent({ type, data });

    expect(result).toEqual({ ok: true, event: { type, data } });
    if (result.ok) expect(result.event.data).toBe(data);
  });

  it("accepts an opaque sequenced event only with trusted session metadata", () => {
    expect(parseWireEvent({
      type: "provider.future.delta",
      data: { session_id: "payload-selected-session" },
      seq: 1,
      session_id: "trusted-session",
    })).toMatchObject({
      ok: true,
      event: { session_id: "trusted-session" },
    });
  });

  it("does not trust a payload-owned session id for a sequenced event", () => {
    expect(parseWireEvent({
      type: "provider.future.delta",
      data: { session_id: "payload-selected-session" },
      seq: 1,
    })).toEqual({
      ok: false,
      error: {
        code: "untrusted_session",
        event_type: "provider.future.delta",
      },
    });
  });

  it.each([
    null,
    [],
    {},
    { type: "", data: {} },
    { type: "pong", data: [] },
    { type: "pong", data: {}, seq: -1, session_id: "session-a" },
    { type: "pong", data: {}, seq: 1.5, session_id: "session-a" },
    { type: "pong", data: {}, session_id: "  " },
  ])("returns an envelope failure for malformed input %#", (value) => {
    expect(parseWireEvent(value)).toMatchObject({
      ok: false,
      error: { code: "invalid_envelope" },
    });
  });

  it("keeps registry proof complete", () => {
    expect(CORE_PAYLOAD_CASES.map(({ type }) => type).sort()).toEqual(
      Object.keys(knownCoreEventValidators).sort(),
    );
  });

  it.each(CORE_PAYLOAD_CASES)("accepts a valid $type core payload", ({ type, valid }) => {
    expect(parseWireEvent({ type, data: valid })).toEqual({
      ok: true,
      event: { type, data: valid },
    });
  });

  it.each(CORE_PAYLOAD_CASES.filter((item) => item.invalid !== undefined))(
    "distinguishes an invalid $type core payload",
    ({ type, invalid }) => {
      expect(parseWireEvent({ type, data: invalid })).toEqual({
        ok: false,
        error: { code: "invalid_core_payload", event_type: type },
      });
    },
  );

  it.each([
    {
      kind: "terminal result",
      data: {
        app_session_id: "session-a",
        success: true,
        session_id: null,
        token_usage: { input_tokens: 3, output_tokens: 2 },
      },
    },
    {
      kind: "turn-manager summary",
      data: {
        app_session_id: "session-a",
        workers_used: ["worker-a"],
        total_token_usage: { input_tokens: 3, output_tokens: 2 },
        trace_id: "trace-a",
      },
    },
  ])("accepts the canonical turn_complete $kind", ({ data }) => {
    expect(parseWireEvent({ type: "turn_complete", data })).toEqual({
      ok: true,
      event: { type: "turn_complete", data },
    });
  });

  it.each([
    ["hybrid", {
      app_session_id: "session-a",
      success: true,
      workers_used: [],
      total_token_usage: {},
      trace_id: "trace-a",
    }],
    ["summary with terminal metadata", {
      app_session_id: "session-a",
      session_id: "provider-session-a",
      workers_used: [],
      total_token_usage: {},
      trace_id: "trace-a",
    }],
    ["non-boolean success", { success: "true" }],
    ["invalid terminal token usage", { success: true, token_usage: { input_tokens: -1 } }],
    ["missing summary owner", {
      workers_used: [],
      total_token_usage: {},
      trace_id: "trace-a",
    }],
    ["invalid worker ids", {
      app_session_id: "session-a",
      workers_used: [7],
      total_token_usage: {},
      trace_id: "trace-a",
    }],
    ["invalid summary token usage", {
      app_session_id: "session-a",
      workers_used: [],
      total_token_usage: { input_tokens: "3" },
      trace_id: "trace-a",
    }],
    ["missing trace id", {
      app_session_id: "session-a",
      workers_used: [],
      total_token_usage: {},
    }],
  ])("rejects a malformed turn_complete %s", (_kind, data) => {
    expect(parseWireEvent({ type: "turn_complete", data })).toEqual({
      ok: false,
      error: { code: "invalid_core_payload", event_type: "turn_complete" },
    });
  });

  it("accepts both provider progress and startup clear variants", () => {
    expect(parseWireEvent({
      type: "provider_install_progress",
      data: { kind: "codex", phase: "started" },
    })).toMatchObject({ ok: true });
    expect(parseWireEvent({
      type: "background_work_changed",
      data: { epoch: "epoch-1", cleared: true },
    })).toMatchObject({ ok: true });
  });

  it("normalizes the historical outer rewind payload", () => {
    expect(parseWireEvent({
      type: "rewind_complete",
      session_id: "session-a",
      messages: [],
    })).toEqual({
      ok: true,
      event: {
        type: "rewind_complete",
        session_id: "session-a",
        data: { session_id: "session-a", messages: [] },
      },
    });
  });

  it("never throws while inspecting hostile input", () => {
    const input = Object.defineProperty({}, "type", {
      get() {
        throw new Error("hostile getter");
      },
    });

    expect(() => parseWireEvent(input)).not.toThrow();
    expect(parseWireEvent(input)).toMatchObject({
      ok: false,
      error: { code: "invalid_envelope" },
    });
  });
});
