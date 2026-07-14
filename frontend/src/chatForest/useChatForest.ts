import { useCallback, useEffect, useReducer, useRef } from "react";
import { API } from "../api";
import { subscribeMany } from "../lib/eventBus";
import { emptyForestState, reduceChatForest } from "./reducer";
import type { ForestResponse } from "./types";

const INVALIDATING_EVENTS = [
  "agent_message", "manager_event", "worker_event", "worker_start",
  "worker_complete", "messages_replay", "messages_delta", "turn_complete",
  "turn_stopped", "session_reconciled", "stub_invalidated",
];

function eventSessionId(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const value = payload as Record<string, unknown>;
  const id = value.app_session_id ?? value.session_id;
  return typeof id === "string" ? id : null;
}

export function useChatForest(sessionId: string | null | undefined) {
  const [state, dispatch] = useReducer(reduceChatForest, emptyForestState);
  const stateRef = useRef(state);
  const inFlight = useRef<Promise<void> | null>(null);
  const queued = useRef(false);
  stateRef.current = state;

  const refresh = useCallback(async (forceSnapshot = false) => {
    if (!sessionId) return;
    if (inFlight.current) {
      queued.current = true;
      return inFlight.current;
    }
    const run = async () => {
      const current = stateRef.current;
      const query = !forceSnapshot && current.epoch && current.status === "ready"
        ? `?epoch=${encodeURIComponent(current.epoch)}&after_revision=${current.revision}`
        : "";
      try {
        const response = await fetch(`${API}/api/sessions/${sessionId}/chat-forest${query}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json() as ForestResponse;
        dispatch({ type: "response", sessionId, response: payload });
        if (payload.found && payload.kind === "delta" && current.epoch !== payload.epoch) {
          queued.current = true;
        }
      } catch (error) {
        dispatch({ type: "error", sessionId, error: error instanceof Error ? error.message : String(error) });
      }
    };
    inFlight.current = run().finally(() => {
      inFlight.current = null;
      if (queued.current) {
        queued.current = false;
        void refresh(stateRef.current.status !== "ready");
      }
    });
    return inFlight.current;
  }, [sessionId]);

  useEffect(() => {
    if (!sessionId) return;
    dispatch({ type: "load", sessionId });
    void refresh(true);
    const handler = (payload: unknown) => {
      const target = eventSessionId(payload);
      if (target === sessionId || target === null) void refresh(false);
    };
    return subscribeMany(INVALIDATING_EVENTS.map((type) => [type, handler]));
  }, [refresh, sessionId]);

  return { ...state, refresh: () => refresh(true) };
}
