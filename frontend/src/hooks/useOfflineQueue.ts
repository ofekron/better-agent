import { useState, useCallback, useRef, useEffect } from "react";
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
  deferUntilTargetReady?: boolean;
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
    | "runner"
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
    | "draft_input"
    | "draft_images"
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

function entrySessionId(entry: OfflineQueueEntry): string {
  return entry.type === "create_session" ? entry.session.id : entry.sessionId;
}

function entryIdentity(entry: OfflineQueueEntry): string {
  // Match the backend and ack-removal semantics: one logical action is the
  // pair of target session + client-minted action id. This lets us merge
  // tabs and re-enqueues without changing the replay/idempotency contract.
  return `${entrySessionId(entry)}\u0000${entry.clientId}`;
}

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

function hasPayloadArray(value: unknown): boolean {
  return Array.isArray(value) && value.length > 0;
}

function isUsableEntry(entry: unknown): entry is OfflineQueueEntry {
  if (!entry || typeof entry !== "object") return false;
  const e = entry as Partial<OfflineQueueEntry> & { type?: string };
  if (typeof e.clientId !== "string" || !e.clientId) return false;

  if (e.type === "create_session") {
    const s = (e as OfflineCreateSessionEntry).session;
    return !!s && typeof s === "object" && typeof s.id === "string" && !!s.id;
  }

  // send_message (type omitted or explicit): text may be empty for an
  // attachment-only prompt, so require a target session plus either prompt text
  // or at least one attachment payload.
  const p = e as OfflinePromptEntry;
  return typeof p.sessionId === "string" && !!p.sessionId
    && typeof p.prompt === "string"
    && (p.prompt.length > 0 || hasPayloadArray(p.images) || hasPayloadArray(p.files));
}

function dedupeEntries(entries: OfflineQueueEntry[]): OfflineQueueEntry[] {
  const latestById = new Map<string, OfflineQueueEntry>();
  const order: string[] = [];
  for (const entry of entries) {
    const id = entryIdentity(entry);
    if (!latestById.has(id)) order.push(id);
    latestById.set(id, entry);
  }
  return order.map((id) => latestById.get(id)!);
}

function parseQueue(raw: string | null): OfflineQueueEntry[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  return dedupeEntries(normalizeQueueEntries(parsed.filter(isUsableEntry)));
}

function readQueue(): OfflineQueueEntry[] {
  try {
    return parseQueue(localStorage.getItem(STORAGE_KEY));
  } catch {
    return [];
  }
}

function writeQueueRaw(queue: OfflineQueueEntry[]): boolean {
  try {
    if (queue.length === 0) {
      localStorage.removeItem(STORAGE_KEY);
    } else {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(queue));
    }
    return true;
  } catch {
    return false;
  }
}

function loadQueue(): OfflineQueueEntry[] {
  const parsed = readQueue();
  // Repair-on-read after validation / normalization / dedupe. Best effort:
  // if storage is unavailable, keep the parsed in-memory view but never throw.
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw && JSON.stringify(parsed) !== raw) writeQueueRaw(parsed);
  } catch {
    // ignored
  }
  return parsed;
}

export function useOfflineQueue() {
  const [queue, setQueue] = useState<OfflineQueueEntry[]>(loadQueue);
  // Synchronous source for `getAll`. Kept in lockstep at mutation sites so
  // we do not need to touch refs during render.
  const queueRef = useRef(queue);
  const [persistFailed, setPersistFailed] = useState(false);

  const commit = useCallback(
    (update: (prev: OfflineQueueEntry[]) => OfflineQueueEntry[]) => {
      // Read-modify-write against fresh disk so this tab never clobbers a
      // concurrent tab's queued action or resurrects one another tab already
      // removed after an explicit backend ack. Because failed writes return
      // false without updating queueRef/state, there are no memory-only queued
      // actions to merge back in.
      const base = readQueue();
      const next = dedupeEntries(update(base));
      const ok = writeQueueRaw(next);
      setPersistFailed(!ok);
      if (!ok) {
        // Fail closed. A pre-ack action that is not actually durable must not
        // appear queued; callers use `false` to keep/restore the visible draft
        // instead of silently losing intent on reload.
        return false;
      }
      queueRef.current = next;
      setQueue(next);
      return true;
    },
    [],
  );

  const enqueue = useCallback(
    (entry: OfflineQueueEntry) => {
      const [normalized] = normalizeQueueEntries([entry]);
      if (!normalized || !isUsableEntry(normalized)) return false;
      return commit((prev) => {
        const id = entryIdentity(normalized);
        return [...prev.filter((e) => entryIdentity(e) !== id), normalized];
      });
    },
    [commit],
  );

  const getAll = useCallback((): OfflineQueueEntry[] => {
    return queueRef.current;
  }, []);

  const remove = useCallback(
    (clientId: string) => {
      return commit((prev) => prev.filter((e) => e.clientId !== clientId));
    },
    [commit],
  );

  const removeBySessionAndClient = useCallback(
    (sessionId: string, clientId: string) => {
      return commit((prev) => prev.filter((entry) => {
        if (entry.clientId !== clientId) return true;
        if (entry.type === "create_session") return entry.session.id !== sessionId;
        return entry.sessionId !== sessionId;
      }));
    },
    [commit],
  );

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key !== null && event.key !== STORAGE_KEY) return;
      const next = readQueue();
      queueRef.current = next;
      setQueue(next);
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  return { queue, enqueue, getAll, remove, removeBySessionAndClient, persistFailed };
}
