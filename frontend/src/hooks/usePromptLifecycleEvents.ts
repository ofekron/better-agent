import { useEffect, useRef } from "react";

import { eventBus, type UserMessageLifecyclePayload } from "../lib/eventBus";
import { logPromptSend } from "../lib/promptSendLog";
import type { PendingOfflineQueueDraft } from "../lib/offlineQueueProjection";
import type { ChatMessage } from "../types";
import type { QueuedBannerState } from "../utils/queuedPrompts";

type MessageStatus = ChatMessage["status"];

export type PromptLifecycleOperations = {
  getFocusedSessionId: () => string | null;
  pendingDraftCount: (sessionId: string) => number;
  takePendingDraft: (
    sessionId: string,
    clientId: string | null | undefined,
  ) => PendingOfflineQueueDraft | null;
  acknowledgeQueue: (
    sessionId: string,
    item: QueuedBannerState,
    revision: number,
  ) => void;
  consumeQueue: (sessionId: string, ids?: readonly string[]) => void;
  clearOfflineDispatch: (sessionId: string, clientId: string) => void;
  removeOfflineAction: (sessionId: string, clientId: string) => void;
  removePending: (clientId: string) => void;
  stampPendingLifecycle: (clientId: string, lifecycleMessageId: string) => void;
  patchMessageStatus: (
    sessionId: string,
    lifecycleMessageId: string,
    status: MessageStatus,
    errorText?: string,
  ) => void;
  markPendingFailed: (lifecycleMessageId: string, errorText?: string) => void;
};

const LIFECYCLE_EVENTS = [
  "user_message_queued",
  "user_message_sent",
  "user_message_received",
  "user_message_done",
  "user_message_failed",
] as const;

export function usePromptLifecycleEvents(operations: PromptLifecycleOperations) {
  const operationsRef = useRef(operations);
  useEffect(() => {
    operationsRef.current = operations;
  }, [operations]);

  useEffect(() => {
    const offQueued = eventBus.subscribe("prompt_queued", (data) => {
      if (!data.app_session_id || !data.queued_id) return;
      const ops = operationsRef.current;
      const pendingDraftCount = ops.pendingDraftCount(data.app_session_id);
      const pendingDraft = ops.takePendingDraft(data.app_session_id, data.client_id);
      logPromptSend("app_prompt_queued", {
        app_session_id: data.app_session_id,
        queued_id: data.queued_id,
        client_id: data.client_id ?? null,
        send_mode: data.send_mode,
        queue_position: data.queue_position,
        pending_queue_drafts: pendingDraftCount,
      });
      ops.acknowledgeQueue(data.app_session_id, {
        id: data.queued_id,
        clientId: data.client_id ?? null,
        preview: pendingDraft?.preview ?? data.prompt_preview ?? "",
        ...(pendingDraft?.images?.length ? { images: pendingDraft.images } : {}),
        ...(pendingDraft?.files?.length ? { files: pendingDraft.files } : {}),
      }, data.queue_revision ?? 0);
      if (!data.client_id) return;
      ops.clearOfflineDispatch(data.app_session_id, data.client_id);
      ops.removeOfflineAction(data.app_session_id, data.client_id);
      ops.removePending(data.client_id);
    });

    const offConsumed = eventBus.subscribe("queue_consumed", (data) => {
      if (!data.app_session_id) return;
      operationsRef.current.consumeQueue(
        data.app_session_id,
        data.queued_id ? [data.queued_id] : undefined,
      );
    });

    const lifecycleOffs = LIFECYCLE_EVENTS.map((type) => (
      eventBus.subscribe(type, (data) => applyLifecycle(type, data, operationsRef.current))
    ));

    return () => {
      offQueued();
      offConsumed();
      for (const off of lifecycleOffs) off();
    };
  }, []);
}

function applyLifecycle(
  type: typeof LIFECYCLE_EVENTS[number],
  data: UserMessageLifecyclePayload,
  operations: PromptLifecycleOperations,
) {
  const sessionId = data.app_session_id ?? operations.getFocusedSessionId();
  if (!sessionId || !data.lifecycle_msg_id) return;
  logPromptSend("app_lifecycle", {
    app_session_id: sessionId,
    event: type,
    lifecycle_msg_id: data.lifecycle_msg_id,
    client_id: data.client_id ?? null,
    kind: data.kind ?? null,
    error: data.error ?? data.reason,
  }, type === "user_message_failed" ? "warn" : "info");

  if (type === "user_message_queued") {
    if (!data.client_id) return;
    operations.clearOfflineDispatch(sessionId, data.client_id);
    operations.removeOfflineAction(sessionId, data.client_id);
    if (data.kind === "queued_behind") {
      operations.removePending(data.client_id);
      return;
    }
    operations.stampPendingLifecycle(data.client_id, data.lifecycle_msg_id);
    return;
  }
  if (type === "user_message_sent") {
    operations.patchMessageStatus(sessionId, data.lifecycle_msg_id, "sending");
    return;
  }
  if (type === "user_message_received") {
    operations.patchMessageStatus(sessionId, data.lifecycle_msg_id, "received");
    return;
  }
  if (type === "user_message_done") {
    operations.patchMessageStatus(sessionId, data.lifecycle_msg_id, undefined);
    return;
  }
  const errorText = data.error ?? data.reason;
  operations.patchMessageStatus(sessionId, data.lifecycle_msg_id, "error", errorText);
  operations.markPendingFailed(data.lifecycle_msg_id, errorText);
}
