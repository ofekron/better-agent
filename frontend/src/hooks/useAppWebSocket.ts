import { useCallback, useEffect, useRef, useState } from "react";

import type { OrchestrationMode, SendMode, WSEvent } from "../types";
import { wireHarnessProfileId } from "../lib/harnessProfile";
import { logPromptSend } from "../lib/promptSendLog";
import { routeAppWebSocketEvent } from "../lib/webSocketProjectors/router";
import type { AppWebSocketOptions } from "../lib/webSocketProjectors/types";
import { useWebSocket } from "./useWebSocket";

export { resolveLiveFrameSessionId } from "../lib/webSocketProjectors/sessionProjector";

export interface ImagePayload {
  data: string;
  media_type: string;
}

export interface FilePayload {
  name: string;
  data: string;
  media_type: string;
  size: number;
}

export type StreamingPhase = "manager" | "worker" | null;
export type StreamingLoadPhase = "starting" | "connected" | null;

export type UseAppWebSocketOptions = AppWebSocketOptions;

export interface UseAppWebSocketReturn {
  connected: boolean;
  /** Verifies the socket immediately instead of waiting for the next
   * heartbeat cycle -- see the checkConnection definition below. */
  checkConnection: () => void;
  sendMessage: (
    prompt: string,
    model: string,
    cwd: string,
    claudeSessionId?: string | null,
    appSessionId?: string | null,
    images?: ImagePayload[],
    orchestrationMode?: OrchestrationMode,
    clientId?: string | null,
    sendMode?: SendMode | null,
    sendTarget?: "worker" | "supervisor" | null,
    files?: FilePayload[],
    harnessProfileId?: string,
  ) => boolean;
  stopStreaming: (appSessionId: string) => boolean;
  sendPromoteQueued: (
    appSessionId: string,
    action?: "interrupt" | "steer",
    queuedId?: string,
    queuedIds?: string[],
  ) => boolean;
  sendCancelQueued: (appSessionId: string, queuedId?: string) => boolean;
  sendUpdateQueued: (
    appSessionId: string,
    queuedId: string,
    content: string
  ) => boolean;
  sendBeginQueuedEdit: (appSessionId: string, queuedId: string) => boolean;
  sendFinishQueuedEdit: (appSessionId: string, queuedId: string) => boolean;
  events: WSEvent[];
  isStreaming: boolean;
  isStopping: boolean;
  streamingPhase: StreamingPhase;
  streamingLoadPhase: StreamingLoadPhase;
  lastResult: Record<string, unknown> | null;
  streamingAppSessionId: string | null;
}

export function useAppWebSocket(
  _url: string,
  options: UseAppWebSocketOptions = {},
): UseAppWebSocketReturn {
  const optionsRef = useRef(options);
  useEffect(() => {
    optionsRef.current = options;
  }, [options]);

  const currentAppSessionIdRef = useRef<string | null>(
    options.currentAppSessionId ?? null,
  );
  useEffect(() => {
    currentAppSessionIdRef.current = options.currentAppSessionId ?? null;
  }, [options.currentAppSessionId]);

  const [events, setEvents] = useState<WSEvent[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isStopping, setIsStopping] = useState(false);
  const [streamingPhase, setStreamingPhase] = useState<StreamingPhase>(null);
  const [streamingLoadPhase, setStreamingLoadPhase] =
    useState<StreamingLoadPhase>(null);
  const streamingLoadPhaseRef = useRef(streamingLoadPhase);
  useEffect(() => {
    streamingLoadPhaseRef.current = streamingLoadPhase;
  }, [streamingLoadPhase]);
  const [lastResult, setLastResult] = useState<Record<string, unknown> | null>(
    null,
  );
  const [streamingAppSessionId, setStreamingAppSessionId] =
    useState<string | null>(null);

  const applyTerminalEvent = useCallback(
    (result: Record<string, unknown> | null) => {
      setIsStreaming(false);
      setIsStopping(false);
      setStreamingPhase(null);
      setStreamingLoadPhase(null);
      setLastResult(result);
    },
    [],
  );

  const onConnectionChanged = useCallback((nextConnected: boolean) => {
    if (nextConnected) return;
    setIsStreaming(false);
    setIsStopping(false);
    setStreamingPhase(null);
    setStreamingLoadPhase(null);
    setStreamingAppSessionId(null);
  }, []);

  const onEvent = useCallback((event: WSEvent): void | Promise<void> => (
    routeAppWebSocketEvent(event, {
      callbacks: optionsRef.current,
      state: {
        currentAppSessionId: currentAppSessionIdRef.current,
        clientId: optionsRef.current.clientId,
        streamingLoadPhase: streamingLoadPhaseRef.current,
        setEvents,
        setIsStreaming,
        setStreamingPhase,
        setStreamingLoadPhase,
        setLastResult,
        setStreamingAppSessionId,
        applyTerminalEvent,
      },
    })
  ), [applyTerminalEvent]);

  const {
    connected,
    checkConnection,
    getReadyState,
    send,
    subscribeSession,
  } = useWebSocket({
    currentAppSessionId: options.currentAppSessionId,
    prepareSessionSubscriptions: options.prepareSessionSubscriptions,
    additionalAppSessionIds: options.additionalAppSessionIds,
    getSinceSeq: options.getSinceSeq,
    getEventsFromSeq: options.getEventsFromSeq,
    getEventsCursorKnown: options.getEventsCursorKnown,
    onEventSeqAdvance: options.onEventSeqAdvance,
    onBeforePublish: options.onAnyEvent,
    onEvent,
    onConnectionChanged,
  });

  const sendMessage = useCallback(
    (
      prompt: string,
      model: string,
      cwd: string,
      claudeSessionId?: string | null,
      appSessionId?: string | null,
      images?: ImagePayload[],
      orchestrationMode?: OrchestrationMode,
      clientId?: string | null,
      sendMode?: SendMode | null,
      sendTarget?: "worker" | "supervisor" | null,
      files?: FilePayload[],
      harnessProfileId?: string,
    ): boolean => {
      const logData = {
        app_session_id: appSessionId ?? null,
        client_id: clientId ?? null,
        send_mode: sendMode ?? null,
        send_target: sendTarget ?? null,
        orchestration_mode: orchestrationMode ?? null,
        prompt_length: prompt.length,
        image_count: images?.length ?? 0,
        file_count: files?.length ?? 0,
        harness_profile_id: harnessProfileId || null,
        ws_state: getReadyState(),
        is_streaming: isStreaming,
      };
      logPromptSend("ws_send_attempt", logData);
      if (getReadyState() !== WebSocket.OPEN) {
        logPromptSend("ws_not_open", logData, "warn");
        return false;
      }

      setStreamingAppSessionId(appSessionId ?? null);
      if (!isStreaming) {
        setEvents([]);
        setIsStreaming(true);
        setStreamingLoadPhase(null);
        setLastResult(null);
      }

      try {
        if (appSessionId) subscribeSession(appSessionId);
        if (!send({
          type: "send_message",
          prompt,
          model,
          cwd,
          session_id: claudeSessionId || null,
          app_session_id: appSessionId || null,
          images: images && images.length > 0 ? images : undefined,
          files: files && files.length > 0 ? files : undefined,
          orchestration_mode: orchestrationMode,
          client_id: clientId || null,
          send_mode: sendMode || undefined,
          send_target: sendTarget || undefined,
          harness_profile_id: wireHarnessProfileId(harnessProfileId),
        })) {
          return false;
        }
      } catch (error) {
        logPromptSend("ws_send_throw", {
          ...logData,
          error: error instanceof Error ? error.message : String(error),
        }, "error");
        return false;
      }
      logPromptSend("ws_send_ok", logData);
      return true;
    },
    [getReadyState, isStreaming, send, subscribeSession],
  );

  const stopStreaming = useCallback((appSessionId: string): boolean => {
    if (!send({ type: "stop_message", app_session_id: appSessionId })) {
      return false;
    }
    setIsStopping(true);
    return true;
  }, [send]);

  const sendPromoteQueued = useCallback((
    appSessionId: string,
    action: "interrupt" | "steer" = "interrupt",
    queuedId?: string,
    queuedIds?: string[],
  ): boolean => send({
    type: "promote_queued",
    app_session_id: appSessionId,
    action,
    queued_id: queuedId,
    queued_ids: queuedIds,
  }), [send]);

  const sendCancelQueued = useCallback((
    appSessionId: string,
    queuedId?: string,
  ): boolean => send({
    type: "cancel_queued",
    app_session_id: appSessionId,
    queued_id: queuedId,
  }), [send]);

  const sendUpdateQueued = useCallback((
    appSessionId: string,
    queuedId: string,
    content: string,
  ): boolean => send({
    type: "update_queued",
    app_session_id: appSessionId,
    queued_id: queuedId,
    content,
  }), [send]);

  const sendBeginQueuedEdit = useCallback((
    appSessionId: string,
    queuedId: string,
  ): boolean => send({
    type: "begin_queued_edit",
    app_session_id: appSessionId,
    queued_id: queuedId,
  }), [send]);

  const sendFinishQueuedEdit = useCallback((
    appSessionId: string,
    queuedId: string,
  ): boolean => send({
    type: "finish_queued_edit",
    app_session_id: appSessionId,
    queued_id: queuedId,
  }), [send]);

  return {
    connected,
    checkConnection,
    sendMessage,
    stopStreaming,
    sendPromoteQueued,
    sendCancelQueued,
    sendUpdateQueued,
    sendBeginQueuedEdit,
    sendFinishQueuedEdit,
    events,
    isStreaming,
    isStopping,
    streamingPhase,
    streamingLoadPhase,
    lastResult,
    streamingAppSessionId,
  };
}
