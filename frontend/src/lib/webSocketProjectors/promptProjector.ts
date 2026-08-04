import type { ChatMessage, WSEvent } from "../../types";
import { logPromptSend } from "../promptSendLog";
import type { ProjectionResult, WebSocketProjectorContext } from "./types";

const LIFECYCLE_EVENTS = new Set([
  "user_message_queued",
  "user_message_sent",
  "user_message_received",
  "user_message_done",
  "user_message_failed",
]);

export function projectPromptEvent(
  event: WSEvent,
  { callbacks, state }: WebSocketProjectorContext,
): ProjectionResult {
  if (event.type === "user_message_persisted") {
    const data = event.data as { session_id?: string; user_message?: ChatMessage };
    if (data.session_id && data.user_message) {
      callbacks.onUserMessagePersisted?.(data.session_id, data.user_message);
    }
    return { stop: true };
  }

  if (event.type === "steer_prompt_persisted") {
    const data = event.data as { app_session_id?: string; client_id?: string | null };
    if (data.app_session_id) {
      callbacks.onSteerPromptPersisted?.(data.app_session_id, data.client_id ?? null);
    }
    return { stop: true };
  }

  if (LIFECYCLE_EVENTS.has(event.type)) {
    const data = event.data as {
      app_session_id?: string;
      lifecycle_msg_id?: string;
      client_id?: string;
      kind?: string;
      error?: string;
      reason?: string;
    };
    const sessionId = data.app_session_id ?? state.currentAppSessionId;
    logPromptSend("lifecycle", {
      event: event.type,
      app_session_id: sessionId,
      lifecycle_msg_id: data.lifecycle_msg_id,
      client_id: data.client_id,
      kind: data.kind,
      error: data.error ?? data.reason,
    }, event.type === "user_message_failed" ? "warn" : "info");
    if (sessionId) callbacks.onUserMsgLifecycle?.(sessionId, event);
    return { stop: true };
  }

  if (event.type === "prompt_queued") {
    const data = event.data as {
      app_session_id?: string;
      queued_id?: string;
      prompt_preview?: string;
      send_mode?: string;
      queue_position?: number;
      client_id?: string;
    };
    logPromptSend("prompt_queued_ack", {
      app_session_id: data.app_session_id,
      queued_id: data.queued_id,
      client_id: data.client_id,
      send_mode: data.send_mode,
      queue_position: data.queue_position,
      preview_length: data.prompt_preview?.length ?? 0,
    });
    if (data.queued_id && data.app_session_id) {
      callbacks.onPromptQueued?.({
        app_session_id: data.app_session_id,
        queued_id: data.queued_id,
        prompt_preview: data.prompt_preview ?? "",
        send_mode: data.send_mode ?? "queue",
        queue_position: data.queue_position ?? 1,
        client_id: data.client_id,
      });
    }
  }

  if (event.type === "queue_consumed") {
    const data = event.data as { app_session_id?: string; queued_id?: string | null };
    logPromptSend("queue_consumed", {
      app_session_id: data.app_session_id,
      queued_id: data.queued_id ?? null,
    });
    if (data.app_session_id) {
      callbacks.onQueueConsumed?.({
        app_session_id: data.app_session_id,
        queued_id: data.queued_id ?? null,
      });
    }
  }

  return {};
}
