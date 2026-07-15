import { useState, useCallback, useRef, useEffect } from "react";
import type { CapabilityContext, OrchestrationMode, SendMode, Session } from "../types";
import type { ImagePayload, FilePayload } from "./useWebSocket";
import { uuidv4 } from "../lib/uuid";
import {
  deleteOfflineAction,
  deleteOfflineActionsForSession,
  importOfflineActions,
  loadOfflineActions,
  offlineActionKey,
  offlineActionKeyFor,
  putOfflineAction,
  updateOfflineAction,
} from "../lib/offlineQueueStore";

export type { ImagePayload };
export type { FilePayload };

const LEGACY_STORAGE_KEY = "better_agent_offline_queue";

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
  editing?: OfflineQueueEditState;
  failure?: OfflineQueueFailureState;
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
  editing?: OfflineQueueEditState;
  failure?: OfflineQueueFailureState;
}

export type OfflineQueueEntry = OfflinePromptEntry | OfflineCreateSessionEntry;

export interface OfflineQueueEditState {
  draftPrompt: string;
}

export interface OfflineQueueFailureState {
  errorText: string;
}

const CANONICAL_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function entrySessionId(entry: OfflineQueueEntry): string {
  return entry.type === "create_session" ? entry.session.id : entry.sessionId;
}

export function offlineEntrySessionId(entry: OfflineQueueEntry): string {
  return entrySessionId(entry);
}

export function offlineEntryIsEditing(entry: OfflineQueueEntry): boolean {
  return typeof entry.editing?.draftPrompt === "string";
}

export function offlineEntryIsHeld(entry: OfflineQueueEntry): boolean {
  return offlineEntryIsEditing(entry) || typeof entry.failure?.errorText === "string";
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

interface LegacyQueueSnapshot {
  entries: OfflineQueueEntry[];
  normalizedRaw: string;
  raw: string;
}

function readLegacyEntries(): LegacyQueueSnapshot | null {
  const raw = localStorage.getItem(LEGACY_STORAGE_KEY);
  if (raw === null) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error("Invalid legacy offline queue");
  }
  if (!Array.isArray(parsed)) throw new Error("Invalid legacy offline queue");
  const entries = normalizeQueueEntries(parsed.filter(isUsableEntry));
  return { entries, normalizedRaw: JSON.stringify(entries), raw };
}

export function useOfflineQueue() {
  const [queue, setQueue] = useState<OfflineQueueEntry[]>([]);
  const queueRef = useRef(queue);
  const [persistFailed, setPersistFailed] = useState(false);
  const [ready, setReady] = useState(false);
  const channelRef = useRef<BroadcastChannel | null>(null);
  const writeTailRef = useRef<Promise<void>>(Promise.resolve());
  const instanceIdRef = useRef(uuidv4());

  const refresh = useCallback(async () => {
    try {
      const next = (await loadOfflineActions()).filter(isUsableEntry);
      queueRef.current = next;
      setQueue(next);
      setPersistFailed(false);
      return true;
    } catch {
      queueRef.current = [];
      setQueue([]);
      setPersistFailed(true);
      return false;
    } finally {
      setReady(true);
    }
  }, []);

  const notifyChanged = useCallback(() => {
    channelRef.current?.postMessage("changed");
    window.dispatchEvent(new CustomEvent("better-agent-offline-queue-changed", {
      detail: instanceIdRef.current,
    }));
  }, []);

  const persist = useCallback((
    operation: () => Promise<void>,
    updateLocal: (items: OfflineQueueEntry[]) => OfflineQueueEntry[],
  ) => {
    const next = updateLocal(queueRef.current);
    queueRef.current = next;
    setQueue(next);
    const write = writeTailRef.current.then(operation, operation);
    writeTailRef.current = write.catch(() => undefined);
    return write.then(() => {
      setPersistFailed(false);
      notifyChanged();
      return true;
    }, () => {
      setPersistFailed(true);
      return false;
    });
  }, [notifyChanged]);

  const enqueue = useCallback(
    async (entry: OfflineQueueEntry) => {
      const [normalized] = normalizeQueueEntries([entry]);
      if (!normalized || !isUsableEntry(normalized)) return false;
      const key = offlineActionKey(normalized);
      return persist(
        () => putOfflineAction(normalized),
        (items) => {
          const index = items.findIndex((item) => offlineActionKey(item) === key);
          if (index < 0) return [...items, normalized];
          return items.map((item, itemIndex) => itemIndex === index ? normalized : item);
        },
      );
    },
    [persist],
  );

  const getAll = useCallback((): OfflineQueueEntry[] => {
    return queueRef.current;
  }, []);

  const persistRemoval = useCallback((key: string) => {
    return persist(
      () => deleteOfflineAction(key),
      (items) => items.filter((item) => offlineActionKey(item) !== key),
    );
  }, [persist]);

  const removeEntry = useCallback(
    (entry: OfflineQueueEntry) => persistRemoval(offlineActionKey(entry)),
    [persistRemoval],
  );

  const beginEdit = useCallback(
    (entry: OfflineQueueEntry) => {
      const key = offlineActionKey(entry);
      const update = (item: OfflineQueueEntry) => ({ ...item, editing: { draftPrompt: item.prompt } });
      return persist(
        () => updateOfflineAction(key, update),
        (items) => items.map((item) => offlineActionKey(item) === key ? update(item) : item),
      );
    },
    [persist],
  );

  const markFailed = useCallback((sessionId: string, clientId: string, errorText: string) => {
    const key = offlineActionKeyFor(sessionId, clientId);
    const update = (item: OfflineQueueEntry): OfflineQueueEntry => ({
      ...item,
      failure: { errorText },
    });
    return persist(
      () => updateOfflineAction(key, update),
      (items) => items.map((item) => offlineActionKey(item) === key ? update(item) : item),
    );
  }, [persist]);

  const retryFailed = useCallback((entry: OfflineQueueEntry) => {
    const key = offlineActionKey(entry);
    const update = (item: OfflineQueueEntry): OfflineQueueEntry => {
      const { failure: _failure, ...rest } = item;
      void _failure;
      return rest;
    };
    return persist(
      () => updateOfflineAction(key, update),
      (items) => items.map((item) => offlineActionKey(item) === key ? update(item) : item),
    );
  }, [persist]);

  const updateEditDraft = useCallback(
    (entry: OfflineQueueEntry, draftPrompt: string) => {
      const key = offlineActionKey(entry);
      const update = (item: OfflineQueueEntry) => item.editing
        ? { ...item, editing: { draftPrompt } }
        : item;
      return persist(
        () => updateOfflineAction(key, update),
        (items) => items.map((item) => offlineActionKey(item) === key ? update(item) : item),
      );
    },
    [persist],
  );

  const finishEdit = useCallback(
    (entry: OfflineQueueEntry) => {
      const key = offlineActionKey(entry);
      const update = (item: OfflineQueueEntry): OfflineQueueEntry => {
        if (!item.editing) return item;
        const prompt = item.editing.draftPrompt;
        const { editing: _editing, failure: _failure, ...rest } = item;
        void _editing;
        void _failure;
        if (rest.type !== "create_session") return { ...rest, prompt };
        const name = prompt ? prompt.split("\n")[0].slice(0, 80) : rest.session.name;
        return { ...rest, prompt, session: { ...rest.session, name } };
      };
      return persist(
        () => updateOfflineAction(key, update),
        (items) => items.map((item) => offlineActionKey(item) === key ? update(item) : item),
      );
    },
    [persist],
  );

  const cancelEdit = useCallback(
    (entry: OfflineQueueEntry) => {
      const key = offlineActionKey(entry);
      const update = (item: OfflineQueueEntry): OfflineQueueEntry => {
        if (!item.editing) return item;
        const { editing: _editing, ...rest } = item;
        void _editing;
        return rest;
      };
      return persist(
        () => updateOfflineAction(key, update),
        (items) => items.map((item) => offlineActionKey(item) === key ? update(item) : item),
      );
    },
    [persist],
  );

  const removeBySessionAndClient = useCallback(
    (sessionId: string, clientId: string) =>
      persistRemoval(offlineActionKeyFor(sessionId, clientId)),
    [persistRemoval],
  );

  // A deleted session must never come back from a stale backlog replay: drop
  // every queued entry (the create itself, and any prompts queued against
  // it) targeting this id, regardless of which client queued them.
  const removeAllForSession = useCallback((sessionId: string) => {
    return persist(
      () => deleteOfflineActionsForSession(sessionId),
      (items) => items.filter((item) => entrySessionId(item) !== sessionId),
    );
  }, [persist]);

  useEffect(() => {
    void (async () => {
      let initializationFailed = false;
      try {
        const legacy = readLegacyEntries();
        if (legacy) {
          if (legacy.normalizedRaw !== legacy.raw) {
            localStorage.setItem(LEGACY_STORAGE_KEY, legacy.normalizedRaw);
          }
          await importOfflineActions(legacy.entries);
          localStorage.removeItem(LEGACY_STORAGE_KEY);
        }
      } catch {
        initializationFailed = true;
      }
      const loaded = await refresh();
      if (initializationFailed || !loaded) setPersistFailed(true);
    })();
    const onChanged = (event?: Event) => {
      if (event instanceof CustomEvent && event.detail === instanceIdRef.current) return;
      void refresh();
    };
    window.addEventListener("better-agent-offline-queue-changed", onChanged);
    if (typeof BroadcastChannel !== "undefined") {
      const channel = new BroadcastChannel("better-agent-offline-actions");
      channel.onmessage = onChanged;
      channelRef.current = channel;
    }
    return () => {
      window.removeEventListener("better-agent-offline-queue-changed", onChanged);
      channelRef.current?.close();
      channelRef.current = null;
    };
  }, [refresh]);

  return {
    markFailed,
    retryFailed,
    queue,
    enqueue,
    getAll,
    removeEntry,
    removeBySessionAndClient,
    removeAllForSession,
    beginEdit,
    updateEditDraft,
    finishEdit,
    cancelEdit,
    persistFailed,
    ready,
  };
}
