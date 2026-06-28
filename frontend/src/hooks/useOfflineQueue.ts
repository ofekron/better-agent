import { useState, useCallback, useRef } from "react";
import type { CapabilityContext, OrchestrationMode, SendMode, Session } from "../types";
import type { ImagePayload, FilePayload } from "./useWebSocket";
import { uuidv4 } from "../lib/uuid";

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
    | "permission"
    | "cwd"
    | "orchestration_mode"
    | "provider_id"
    | "browser_harness_enabled"
    | "browser_harness_headless"
    | "node_id"
    | "created_at"
    | "updated_at"
    | "messages"
    | "capability_contexts"
    | "folder_id"
  >;
  prompt: string;
  images?: ImagePayload[];
  files?: FilePayload[];
  capabilityContexts?: CapabilityContext[];
}

export type OfflineQueueEntry = OfflinePromptEntry | OfflineCreateSessionEntry;

const STORAGE_KEY = "better_agent_offline_queue";

const CANONICAL_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/** A `create_session` entry persisted by older code (or minted in a context
 * where UUID generation was broken) can carry a non-canonical `session.id`.
 * The backend rejects it as `client_session_id` (400), so the entry would
 * 400-loop forever on every reconnect flush. Re-mint a canonical UUID,
 * preserving the queued prompt/config — never drop the user's intent. */
export function normalizeQueueEntries(entries: OfflineQueueEntry[]): OfflineQueueEntry[] {
  return entries.map((entry) => {
    if (entry.type !== "create_session") return entry;
    if (CANONICAL_UUID.test(entry.session.id)) return entry;
    return { ...entry, session: { ...entry.session, id: uuidv4() } };
  });
}

function loadQueue(): OfflineQueueEntry[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as OfflineQueueEntry[];
    const normalized = normalizeQueueEntries(parsed);
    if (JSON.stringify(normalized) !== JSON.stringify(parsed)) {
      saveQueue(normalized);
    }
    return normalized;
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

  return { queue, enqueue, getAll, remove, removeBySessionAndClient };
}
