import { logDurable, logDurableImmediate } from "./frontendLogger";

type Direction = "inbound" | "outbound";
type FlushReason = "periodic" | "connection_closed" | "unmounted";

type TrafficAggregate = {
  frames: number;
  bytes: number;
  failures: number;
  processing_ms_total: number;
  processing_ms_max: number;
  buffered_bytes_max: number;
};

type TrafficSummary = {
  interval_ms: number;
  reason: FlushReason;
  summary_bytes: number;
  inbound: Record<string, TrafficAggregate>;
  outbound: Record<string, TrafficAggregate>;
};

const FLUSH_INTERVAL_MS = 15_000;
const MAX_EVENT_TYPES = 64;
const MAX_SUMMARY_BYTES = 12_000;
export const KNOWN_WEB_SOCKET_EVENT_TYPES = new Set([
  "agent_message", "thinking", "tool_call", "output", "steer_prompt",
  "session_discovered", "complete", "error", "turn_start", "manager_event",
  "model_switched", "model_fallback", "turn_complete", "user_message_persisted",
  "steer_prompt_persisted", "messages_replay", "snapshot_begin", "snapshot_chunk",
  "snapshot_end", "snapshot_restart_required", "snapshot_refresh_required",
  "snapshot_refresh_complete", "snapshot_cancelled", "stub_invalidated",
  "messages_delta", "run_state", "worker_start", "worker_event", "worker_complete",
  "worker_creation_requested", "worker_creation_approved", "worker_creation_failed",
  "user_input_requested", "user_input_resolved", "worker_prep_start",
  "worker_prep_event", "worker_prep_complete", "worker_prep_cancelled",
  "workers_changed", "session_organization_changed", "user_prefs_changed",
  "ui_selection_changed", "credential_consent_changed", "projects_changed",
  "project_updates_changed", "project_mappings_changed", "turn_started", "turn_stopped",
  "turn_detached", "session_renamed", "rewind_complete", "tool_approval_requested",
  "tool_approval_resolved", "session_metadata_updated", "session_forked",
  "session_created", "provider_changed", "provider_install_progress",
  "provider_install_finished", "provider_config_sync_changed", "extensions_changed",
  "extension_updates_changed", "models_catalog_changed", "prompt_queued",
  "supervisor_event", "user_message_queued", "user_message_sent",
  "user_message_received", "user_message_done", "user_message_failed",
  "message_recovering_changed", "message_retrying_changed", "message_auto_retry_changed",
  "message_content_updated", "message_continuation_changed", "message_run_meta_changed",
  "message_ask_result_changed", "message_ask_choice_changed", "session_processing_started",
  "session_processing_finished", "session_reconciled", "session_running_changed",
  "session_monitoring_changed", "session_unread_changed", "session_provenance_changed",
  "session_error_changed", "session_user_input_changed", "session_marker_changed",
  "node_state_changed", "node_registration_requested", "node_registration_resolved",
  "session_deleted", "diagnostic", "lifecycle_notice", "tool_result", "pr_link",
  "startup_task_changed", "command_received", "queue_consumed", "todos_snapshot",
  "schedules_updated", "schedules_changed", "internal_llm_changed", "tasks_changed",
  "extension_event", "switch_control_state_changed", "subscribe", "unsubscribe",
  "send_message", "stop_message", "promote_queued", "cancel_queued", "update_queued",
  "begin_queued_edit", "finish_queued_edit", "snapshot_resume", "snapshot_ack",
  "snapshot_refresh",
]);

function emitTrafficSummary(
  source: string,
  stage: string,
  data: Record<string, unknown>,
): void {
  const reason = data.reason;
  if (reason === "connection_closed" || reason === "unmounted") {
    logDurableImmediate(source, stage, data);
    return;
  }
  logDurable(source, stage, data);
}

const emptyAggregate = (): TrafficAggregate => ({
  frames: 0,
  bytes: 0,
  failures: 0,
  processing_ms_total: 0,
  processing_ms_max: 0,
  buffered_bytes_max: 0,
});

export function utf8ByteLength(value: string): number {
  let bytes = 0;
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code <= 0x7f) {
      bytes += 1;
      continue;
    }
    if (code <= 0x7ff) {
      bytes += 2;
      continue;
    }
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        bytes += 4;
        index += 1;
        continue;
      }
    }
    bytes += 3;
  }
  return bytes;
}

export function webSocketDataBytes(data: unknown): number {
  if (typeof data === "string") return utf8ByteLength(data);
  if (data instanceof Blob) return data.size;
  if (data instanceof ArrayBuffer) return data.byteLength;
  if (ArrayBuffer.isView(data)) return data.byteLength;
  return 0;
}

export class WebSocketTrafficLog {
  private readonly inbound = new Map<string, TrafficAggregate>();
  private readonly outbound = new Map<string, TrafficAggregate>();
  private readonly seenTypes = new Set<string>();
  private readonly emit: (
    source: string,
    stage: string,
    data: Record<string, unknown>,
  ) => void;
  private readonly now: () => number;
  private readonly flushIntervalMs: number;
  private startedAt: number;
  private dirty = false;

  constructor(
    emit: (
      source: string,
      stage: string,
      data: Record<string, unknown>,
    ) => void = emitTrafficSummary,
    now: () => number = () => performance.now(),
    flushIntervalMs = FLUSH_INTERVAL_MS,
  ) {
    this.emit = emit;
    this.now = now;
    this.flushIntervalMs = flushIntervalMs;
    this.startedAt = this.now();
  }

  recordInbound(
    eventType: unknown,
    bytes: number,
    processingMs: number,
    failed: boolean,
  ): void {
    this.record("inbound", eventType, bytes, processingMs, 0, failed);
  }

  recordOutbound(
    eventType: unknown,
    bytes: number,
    bufferedBytes: number,
    failed: boolean,
  ): void {
    this.record("outbound", eventType, bytes, 0, bufferedBytes, failed);
  }

  flush(reason: FlushReason): void {
    if (!this.dirty) return;
    const now = this.now();
    const summary = {
      interval_ms: Math.max(0, Math.round(now - this.startedAt)),
      reason,
      summary_bytes: 0,
      inbound: this.toRecord(this.inbound),
      outbound: this.toRecord(this.outbound),
    } satisfies TrafficSummary;
    this.boundSummary(summary);
    try {
      this.emit("websocket-traffic", "summary", summary);
    } catch (error) {
      void error;
    }
    this.inbound.clear();
    this.outbound.clear();
    this.seenTypes.clear();
    this.startedAt = now;
    this.dirty = false;
  }

  private record(
    direction: Direction,
    eventType: unknown,
    bytes: number,
    processingMs: number,
    bufferedBytes: number,
    failed: boolean,
  ): void {
    const type = this.normalizeType(eventType);
    const target = direction === "inbound" ? this.inbound : this.outbound;
    const aggregate = target.get(type) ?? emptyAggregate();
    aggregate.frames += 1;
    aggregate.bytes += this.nonNegative(bytes);
    aggregate.failures += failed ? 1 : 0;
    aggregate.processing_ms_total += this.nonNegative(processingMs);
    aggregate.processing_ms_max = Math.max(
      aggregate.processing_ms_max,
      this.nonNegative(processingMs),
    );
    aggregate.buffered_bytes_max = Math.max(
      aggregate.buffered_bytes_max,
      this.nonNegative(bufferedBytes),
    );
    target.set(type, aggregate);
    this.dirty = true;
    if (this.now() - this.startedAt >= this.flushIntervalMs) {
      this.flush("periodic");
    }
  }

  private normalizeType(value: unknown): string {
    if (typeof value !== "string" || !KNOWN_WEB_SOCKET_EVENT_TYPES.has(value)) return "other";
    if (this.seenTypes.has(value)) return value;
    if (this.seenTypes.size >= MAX_EVENT_TYPES) return "other";
    this.seenTypes.add(value);
    return value;
  }

  private nonNegative(value: number): number {
    if (!Number.isFinite(value) || value <= 0) return 0;
    return Math.round(value * 100) / 100;
  }

  private toRecord(source: Map<string, TrafficAggregate>): Record<string, TrafficAggregate> {
    return Object.fromEntries([...source.entries()].sort(([left], [right]) => (
      left.localeCompare(right)
    )));
  }

  private boundSummary(summary: TrafficSummary): void {
    for (;;) {
      this.measureSummary(summary);
      if (summary.summary_bytes <= MAX_SUMMARY_BYTES) return;
      const inboundKeys = Object.keys(summary.inbound).filter((key) => key !== "other");
      const outboundKeys = Object.keys(summary.outbound).filter((key) => key !== "other");
      const target = inboundKeys.length >= outboundKeys.length
        ? summary.inbound
        : summary.outbound;
      const keys = target === summary.inbound ? inboundKeys : outboundKeys;
      const key = keys.at(-1);
      if (!key) {
        summary.inbound = {};
        summary.outbound = {};
        this.measureSummary(summary);
        return;
      }
      const removed = target[key];
      delete target[key];
      target.other = this.mergeAggregates(target.other, removed);
    }
  }

  private measureSummary(summary: TrafficSummary): void {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const measured = utf8ByteLength(JSON.stringify(summary));
      if (measured === summary.summary_bytes) return;
      summary.summary_bytes = measured;
    }
  }

  private mergeAggregates(
    left: TrafficAggregate | undefined,
    right: TrafficAggregate,
  ): TrafficAggregate {
    const merged = left ?? emptyAggregate();
    merged.frames += right.frames;
    merged.bytes += right.bytes;
    merged.failures += right.failures;
    merged.processing_ms_total += right.processing_ms_total;
    merged.processing_ms_max = Math.max(merged.processing_ms_max, right.processing_ms_max);
    merged.buffered_bytes_max = Math.max(merged.buffered_bytes_max, right.buffered_bytes_max);
    return merged;
  }
}

export const webSocketTrafficLog = new WebSocketTrafficLog();

export function sendWebSocketFrame(
  socket: Pick<WebSocket, "send" | "bufferedAmount">,
  frame: Record<string, unknown>,
  trafficLog = webSocketTrafficLog,
): void {
  const eventType = frame.type;
  let text = "";
  try {
    text = JSON.stringify(frame);
    socket.send(text);
    trafficLog.recordOutbound(
      eventType,
      utf8ByteLength(text),
      readBufferedAmount(socket),
      false,
    );
  } catch (error) {
    trafficLog.recordOutbound(eventType, utf8ByteLength(text), readBufferedAmount(socket), true);
    throw error;
  }
}

function readBufferedAmount(socket: Pick<WebSocket, "bufferedAmount">): number {
  try {
    return socket.bufferedAmount;
  } catch {
    return 0;
  }
}
