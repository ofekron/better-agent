import type { ChatMessage, RunInfo, Session, WSEvent } from "../../types";
import { logPromptSend } from "../promptSendLog";
import type { ProjectionResult, WebSocketProjectorContext } from "./types";

const LIVE_EVENT_TYPES = new Set([
  "agent_message",
  "manager_event",
  "model_switched",
  "steer_prompt",
  "worker_event",
  "turn_start",
  "turn_complete",
  "worker_start",
  "worker_complete",
  "worker_prep_start",
  "worker_prep_event",
  "worker_prep_complete",
  "worker_prep_cancelled",
  "todos_snapshot",
]);

export function resolveLiveFrameSessionId(
  event: WSEvent,
  focusedSessionId?: string | null,
): string | null {
  void focusedSessionId;
  const data = event.data as { app_session_id?: unknown; session_id?: unknown };
  if (typeof data.app_session_id === "string" && data.app_session_id) {
    return data.app_session_id;
  }
  if (
    event.type === "todos_snapshot"
    && typeof data.session_id === "string"
    && data.session_id
  ) {
    return data.session_id;
  }
  return null;
}

function terminalSessionId(event: WSEvent, focusedSessionId: string | null): string | null {
  const data = event.data as { app_session_id?: string };
  return data.app_session_id ?? focusedSessionId;
}

export function projectSessionEvent(
  event: WSEvent,
  { callbacks, state }: WebSocketProjectorContext,
): ProjectionResult {
  const data = event.data as Record<string, unknown>;

  if (event.type === "messages_replay" || event.type === "messages_delta") {
    const sessionId = data.app_session_id;
    const messages = data.messages;
    if (typeof sessionId === "string" && Array.isArray(messages)) {
      const apply = event.type === "messages_replay"
        ? callbacks.onMessagesReplay
        : callbacks.onMessagesDelta;
      apply?.(sessionId, messages as ChatMessage[]);
    }
    return { stop: true };
  }

  if (event.type === "session_reconciled") {
    const rootId = data.root_id;
    return {
      stop: true,
      completion: typeof rootId === "string"
        ? callbacks.onSessionReconciled?.(rootId)
        : undefined,
    };
  }

  if (event.type === "extension_event") {
    const eventName = data.event_name;
    const inner = data.data;
    if (eventName === "extension_toast" && inner && typeof inner === "object") {
      const toast = inner as { message?: unknown; level?: unknown };
      if (typeof toast.message === "string" && toast.message) {
        callbacks.onExtensionToast?.({
          sessionId: event.session_id,
          message: toast.message,
          level: typeof toast.level === "string" ? toast.level : undefined,
        });
      }
    }
    return { stop: true };
  }

  if (event.type === "run_state") {
    const sessionId = data.app_session_id;
    const runs = data.runs;
    if (typeof sessionId === "string" && Array.isArray(runs)) {
      callbacks.onRunState?.(sessionId, runs as RunInfo[]);
    }
    return { stop: true };
  }

  if (event.type === "agent_message" && data.type === "pr-link" && typeof data.prUrl === "string") {
    callbacks.onPrLink?.({
      sessionId: typeof data.app_session_id === "string" ? data.app_session_id : undefined,
      prNumber: typeof data.prNumber === "number" ? data.prNumber : undefined,
      prUrl: data.prUrl,
      prRepository: typeof data.prRepository === "string" ? data.prRepository : undefined,
    });
    return { stop: true };
  }

  if (LIVE_EVENT_TYPES.has(event.type)) {
    const sessionId = resolveLiveFrameSessionId(event);
    if (sessionId) callbacks.onLiveTurnEvent?.(sessionId, event);
  }

  if (event.type === "turn_start") {
    state.setEvents([]);
    state.setIsStreaming(true);
    state.setStreamingPhase("manager");
    state.setStreamingLoadPhase("starting");
    state.setLastResult(null);
    const sessionId = typeof data.app_session_id === "string" ? data.app_session_id : "";
    if (!sessionId) return { stop: true };
    state.setStreamingAppSessionId(sessionId);
    callbacks.onTurnStarted?.(sessionId);
  }

  if (event.type === "worker_start") state.setStreamingPhase("worker");
  if (event.type === "worker_complete") state.setStreamingPhase("manager");

  if (event.type === "agent_message" || event.type === "manager_event") {
    const wrapped = data.event;
    const innerType = event.type === "manager_event"
      && wrapped && typeof wrapped === "object"
      ? (wrapped as { type?: unknown }).type
      : data.type;
    if (innerType === "session_discovered") {
      state.setStreamingLoadPhase("connected");
    } else if (innerType === "assistant" && state.streamingLoadPhase) {
      state.setStreamingLoadPhase(null);
    }
  }

  state.setEvents((previous) => [...previous, event]);

  if (event.type === "turn_complete") {
    state.applyTerminalEvent(data);
    const sessionId = terminalSessionId(event, state.currentAppSessionId);
    if (sessionId) callbacks.onTurnTerminal?.(sessionId);
  }

  if (event.type === "turn_stopped") {
    state.applyTerminalEvent(data);
    const sessionId = terminalSessionId(event, state.currentAppSessionId);
    if (sessionId) {
      callbacks.onTurnTerminal?.(
        sessionId,
        typeof data.stopped_at === "string" ? data.stopped_at : undefined,
        typeof data.interrupted_by_msg_id === "string" ? data.interrupted_by_msg_id : undefined,
      );
    }
  }

  if (event.type === "turn_detached") {
    state.applyTerminalEvent(null);
    const sessionId = terminalSessionId(event, state.currentAppSessionId);
    if (sessionId) callbacks.onTurnDetached?.(sessionId);
  }

  if (event.type === "error") {
    const errorText = typeof data.error === "string" ? data.error : "";
    const clientId = typeof data.client_id === "string" ? data.client_id : null;
    const sessionId = (
      typeof data.app_session_id === "string" ? data.app_session_id
        : typeof data.session_id === "string" ? data.session_id
          : state.currentAppSessionId
    );
    state.applyTerminalEvent({ success: false, error: errorText, client_id: clientId });
    logPromptSend("backend_error", {
      app_session_id: sessionId,
      client_id: clientId,
      error: errorText,
    }, "error");
    if (sessionId && clientId) callbacks.onPromptSendError?.(sessionId, clientId, errorText);
    if (sessionId) callbacks.onTurnTerminal?.(sessionId, undefined, undefined, errorText);
  }

  if (event.type === "rewind_complete") {
    const sessionId = data.session_id;
    const messages = data.messages;
    if (typeof sessionId === "string" && Array.isArray(messages)) {
      callbacks.onRewindComplete?.(sessionId, messages as ChatMessage[]);
    }
  }

  if (event.type === "message_recovering_changed") {
    if (typeof data.session_id === "string" && typeof data.msg_id === "string") {
      callbacks.onMessageRecoveringChanged?.(data.session_id, data.msg_id, Boolean(data.value));
    }
  }
  if (event.type === "message_retrying_changed") {
    if (typeof data.session_id === "string" && typeof data.msg_id === "string") {
      callbacks.onMessageRetryingChanged?.(
        data.session_id,
        data.msg_id,
        typeof data.retry_at === "string" ? data.retry_at : null,
        typeof data.error_text === "string" ? data.error_text : null,
      );
    }
  }
  if (event.type === "message_error_changed") {
    if (typeof data.session_id === "string" && typeof data.msg_id === "string") {
      callbacks.onMessageErrorChanged?.(
        data.session_id,
        data.msg_id,
        typeof data.error_text === "string" ? data.error_text : null,
      );
    }
  }
  if (event.type === "message_auto_retry_changed") {
    if (typeof data.session_id === "string" && typeof data.msg_id === "string") {
      callbacks.onMessageAutoRetryChanged?.(
        data.session_id,
        data.msg_id,
        data.auto_retry && typeof data.auto_retry === "object"
          ? data.auto_retry as { count: number; kind: string }
          : null,
      );
    }
  }
  if (event.type === "message_content_updated") {
    if (typeof data.session_id === "string" && typeof data.msg_id === "string") {
      callbacks.onMessageContentUpdated?.(
        data.session_id,
        data.msg_id,
        typeof data.content === "string" ? data.content : "",
      );
    }
  }
  if (event.type === "message_continuation_changed") {
    if (typeof data.session_id === "string" && typeof data.msg_id === "string") {
      callbacks.onMessageContinuationChanged?.(
        data.session_id,
        data.msg_id,
        typeof data.chain_depth === "number" ? data.chain_depth : null,
      );
    }
  }
  if (event.type === "message_run_meta_changed") {
    if (typeof data.session_id === "string" && typeof data.msg_id === "string") {
      callbacks.onMessageRunMetaChanged?.(
        data.session_id,
        data.msg_id,
        data.run_meta as ChatMessage["run_meta"] ?? null,
      );
    }
  }
  if (event.type === "message_ask_result_changed") {
    if (typeof data.session_id === "string" && typeof data.msg_id === "string") {
      callbacks.onMessageAskResultChanged?.(
        data.session_id,
        data.msg_id,
        data.ask_result as ChatMessage["ask_result"] ?? null,
      );
    }
  }
  if (event.type === "message_ask_choice_changed") {
    if (typeof data.session_id === "string" && typeof data.msg_id === "string") {
      callbacks.onMessageAskChoiceChanged?.(
        data.session_id,
        data.msg_id,
        typeof data.chosen_session_id === "string" ? data.chosen_session_id : null,
      );
    }
  }

  if (event.type === "session_metadata_updated") {
    if (
      typeof data.session_id === "string"
      && data.patch && typeof data.patch === "object"
      && (data.originated_by == null || data.originated_by !== state.clientId)
    ) {
      callbacks.onSessionMetadataUpdated?.(
        data.session_id,
        data.patch as Parameters<NonNullable<typeof callbacks.onSessionMetadataUpdated>>[1],
      );
    }
  }
  if (event.type === "session_created" && data.session && typeof data.session === "object") {
    callbacks.onSessionCreated?.(data.session as Session);
  }
  if (event.type === "session_deleted" && typeof data.session_id === "string") {
    callbacks.onSessionDeleted?.(data.session_id);
  }
  if (event.type === "session_forked" && data.session && typeof data.session === "object") {
    callbacks.onSessionForked?.(
      data.session as Session,
      typeof data.parent_session_id === "string" ? data.parent_session_id : null,
    );
  }
  if (
    event.type === "session_renamed"
    && typeof data.session_id === "string"
    && typeof data.name === "string"
  ) {
    callbacks.onSessionRenamed?.(data.session_id, data.name);
  }
  if (event.type === "supervisor_event") {
    callbacks.onSupervisorEvent?.({
      sessionId: typeof data.session_id === "string" ? data.session_id : undefined,
      kind: typeof data.kind === "string" ? data.kind : "unknown",
      message: typeof data.message === "string" ? data.message : undefined,
      error: typeof data.error === "string" ? data.error : undefined,
      reason: typeof data.reason === "string" ? data.reason : undefined,
    });
  }

  if (event.type === "projects_changed") callbacks.onProjectsChanged?.();
  if (
    event.type === "project_updates_changed"
    && typeof data.project_id === "string"
    && typeof data.unseen_count === "number"
  ) {
    callbacks.onProjectUpdatesChanged?.({
      project_id: data.project_id,
      unseen_count: data.unseen_count,
    });
  }
  if (event.type === "workers_changed") callbacks.onWorkersChanged?.();
  if (event.type === "session_organization_changed") callbacks.onSessionOrganizationChanged?.();
  // `project_mappings_changed` deleted (confirmed dead code, ADR 0008
  // Package B plan §1) — see types.ts's WSEvent union note.

  return {};
}
