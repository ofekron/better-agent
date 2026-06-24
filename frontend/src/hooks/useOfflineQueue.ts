import { useState, useCallback, useRef } from "react";
import type { CapabilityContext, OrchestrationMode, SendMode, Session } from "../types";
import type { ImagePayload, FilePayload } from "./useWebSocket";

export type { ImagePayload };
export type { FilePayload };

export interface OfflinePromptEntry {
  type?: "send_message";
  sessionId: string;
  clientId: string;
  prompt: string;
  model: string;
  cwd: string;
  images?: ImagePayload[];
  files?: FilePayload[];
  orchestrationMode?: OrchestrationMode;
  sendMode?: SendMode | null;
  sendTarget?: "worker" | "supervisor" | null;
  capabilityContexts?: CapabilityContext[];
}

export interface OfflineCreateSessionEntry {
  type: "create_session";
  clientId: string;
  session: Pick<
    Session,
    | "id"
    | "name"
    | "model"
    | "reasoning_effort"
    | "cwd"
    | "orchestration_mode"
    | "provider_id"
    | "browser_test_enabled"
    | "browser_test_headless"
    | "node_id"
    | "created_at"
    | "updated_at"
    | "messages"
    | "capability_contexts"
  >;
  prompt: string;
  images?: ImagePayload[];
  files?: FilePayload[];
  capabilityContexts?: CapabilityContext[];
}

export type OfflineQueueEntry = OfflinePromptEntry | OfflineCreateSessionEntry;

const STORAGE_KEY = "better_agent_offline_queue";

function loadQueue(): OfflineQueueEntry[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function saveQueue(queue: OfflineQueueEntry[]) {
  if (queue.length === 0) {
    localStorage.removeItem(STORAGE_KEY);
  } else {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(queue));
  }
}

export function useOfflineQueue() {
  const [queue, setQueue] = useState<OfflineQueueEntry[]>(loadQueue);
  const queueRef = useRef(queue);
  queueRef.current = queue;

  const mutate = useCallback(
    (update: (prev: OfflineQueueEntry[]) => OfflineQueueEntry[]) => {
      setQueue((prev) => {
        const next = update(prev);
        saveQueue(next);
        queueRef.current = next;
        return next;
      });
    },
    [],
  );

  const enqueue = useCallback((entry: OfflineQueueEntry) => {
    mutate((prev) => [...prev, entry]);
  }, [mutate]);

  const getAll = useCallback((): OfflineQueueEntry[] => {
    return queueRef.current;
  }, []);

  const remove = useCallback((clientId: string) => {
    mutate((prev) => prev.filter((e) => e.clientId !== clientId));
  }, [mutate]);

  const removeBySessionAndClient = useCallback((sessionId: string, clientId: string) => {
    mutate((prev) => prev.filter((entry) => {
      if (entry.clientId !== clientId) return true;
      if (entry.type === "create_session") return entry.session.id !== sessionId;
      return entry.sessionId !== sessionId;
    }));
  }, [mutate]);

  const replaceBySession = useCallback((sessionId: string, entry: OfflinePromptEntry) => {
    mutate((prev) => {
      const index = prev.findIndex(
        (item) => item.type !== "create_session" && item.sessionId === sessionId,
      );
      if (index < 0) return [...prev, entry];
      const next = prev.filter(
        (item) => item.type === "create_session" || item.sessionId !== sessionId,
      );
      next.splice(index, 0, entry);
      return next;
    });
  }, [mutate]);

  return { queue, enqueue, getAll, remove, removeBySessionAndClient, replaceBySession };
}
