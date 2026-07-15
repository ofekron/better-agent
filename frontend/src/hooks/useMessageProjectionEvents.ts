import { useEffect, useRef } from "react";

import { eventBus } from "../lib/eventBus";
import type { AskResult, ChatMessage } from "../types";

export type MessageProjectionOperations = {
  applyRecovering: (sessionId: string, messageId: string, value: boolean) => void;
  applyRetrying: (
    sessionId: string,
    messageId: string,
    retryAt: string | null,
    errorText: string | null,
  ) => void;
  applyAutoRetry: (
    sessionId: string,
    messageId: string,
    autoRetry: { count: number; kind: string } | null,
  ) => void;
  applyContent: (sessionId: string, messageId: string, content: string) => void;
  applyContinuation: (
    sessionId: string,
    messageId: string,
    chainDepth: number | null,
  ) => void;
  applyRunMeta: (
    sessionId: string,
    messageId: string,
    runMeta: ChatMessage["run_meta"],
  ) => void;
  applyAskResult: (
    sessionId: string,
    messageId: string,
    askResult: AskResult | null,
  ) => void;
  applyAskChoice: (
    sessionId: string,
    messageId: string,
    chosenSessionId: string | null,
  ) => void;
  applySessionProcessing: (rootId: string, kind: "started" | "finished") => void;
};

export function useMessageProjectionEvents(operations: MessageProjectionOperations) {
  const operationsRef = useRef(operations);
  useEffect(() => {
    operationsRef.current = operations;
  }, [operations]);

  useEffect(() => {
    const offs = [
      eventBus.subscribe("message_recovering_changed", (data) => {
        if (!data.session_id || !data.msg_id) return;
        operationsRef.current.applyRecovering(data.session_id, data.msg_id, !!data.value);
      }),
      eventBus.subscribe("message_retrying_changed", (data) => {
        if (!data.session_id || !data.msg_id) return;
        operationsRef.current.applyRetrying(
          data.session_id,
          data.msg_id,
          data.retry_at ?? null,
          data.error_text ?? null,
        );
      }),
      eventBus.subscribe("message_auto_retry_changed", (data) => {
        if (!data.session_id || !data.msg_id) return;
        operationsRef.current.applyAutoRetry(
          data.session_id,
          data.msg_id,
          data.auto_retry ?? null,
        );
      }),
      eventBus.subscribe("message_content_updated", (data) => {
        if (!data.session_id || !data.msg_id) return;
        operationsRef.current.applyContent(data.session_id, data.msg_id, data.content ?? "");
      }),
      eventBus.subscribe("message_continuation_changed", (data) => {
        if (!data.session_id || !data.msg_id) return;
        operationsRef.current.applyContinuation(
          data.session_id,
          data.msg_id,
          data.chain_depth ?? null,
        );
      }),
      eventBus.subscribe("message_run_meta_changed", (data) => {
        if (!data.session_id || !data.msg_id) return;
        operationsRef.current.applyRunMeta(data.session_id, data.msg_id, data.run_meta ?? null);
      }),
      eventBus.subscribe("message_ask_result_changed", (data) => {
        if (!data.session_id || !data.msg_id) return;
        operationsRef.current.applyAskResult(
          data.session_id,
          data.msg_id,
          data.ask_result ?? null,
        );
      }),
      eventBus.subscribe("message_ask_choice_changed", (data) => {
        if (!data.session_id || !data.msg_id) return;
        operationsRef.current.applyAskChoice(
          data.session_id,
          data.msg_id,
          data.chosen_session_id ?? null,
        );
      }),
      eventBus.subscribe("session_processing_started", (data) => {
        if (!data.root_id) return;
        operationsRef.current.applySessionProcessing(data.root_id, "started");
      }),
      eventBus.subscribe("session_processing_finished", (data) => {
        if (!data.root_id) return;
        operationsRef.current.applySessionProcessing(data.root_id, "finished");
      }),
    ];
    return () => {
      for (const off of offs) off();
    };
  }, []);
}
