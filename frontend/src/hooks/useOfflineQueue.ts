import { useState, useCallback, useRef, useEffect } from "react";
import type { CapabilityContext, OrchestrationMode, SendMode, Session } from "../types";
import type { ImagePayload, FilePayload } from "./useWebSocket";
import { uuidv4 } from "../lib/uuid";
import { OFFLINE_TAB_ID } from "../lib/offlineFlushLock";
import {
  clearMobileBackgroundAcknowledgements,
  getMobileBackgroundAcknowledgements,
  scheduleMobileBackgroundSyncMirror,
} from "../lib/mobileBackgroundSync";
import { App as CapacitorApp } from "@capacitor/app";
import { Capacitor } from "@capacitor/core";

export type { ImagePayload };
export type { FilePayload };

export interface OfflinePromptEntry {
  type?: "send_message";
  /** Id of the tab that put this action on the wire and is waiting for its
   * ack. Shared across tabs through the same storage record as the entry, so
   * a sibling tab's flush never re-sends an action already in flight. */
  dispatchedBy?: string;
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
  harnessProfileId?: string;
  deferUntilTargetReady?: boolean;
}

/** Exact wire shape of a queued create's session snapshot. Sessions are
 * profile-based: identity travels as `runtime_profile_id` plus the
 * model/effort overrides — never as raw provider/runner. The backend batch
 * validator rejects unknown fields, so this list is the single source for
 * both the compile-time type and the runtime projection below. */
const CREATE_SESSION_PAYLOAD_KEYS = [
  "id",
  "name",
  "model",
  "reasoning_effort",
  "permission",
  "cwd",
  "orchestration_mode",
  "runtime_profile_id",
  "node_id",
  "created_at",
  "updated_at",
  "messages",
  "capability_contexts",
  "harness_profile_id",
  "folder_id",
  "draft_input",
  "draft_images",
] as const;

export type OfflineCreateSessionPayload = Pick<
  Session,
  (typeof CREATE_SESSION_PAYLOAD_KEYS)[number]
>;

export interface OfflineCreateSessionEntry {
  type: "create_session";
  dispatchedBy?: string;
  clientId: string;
  session: OfflineCreateSessionPayload;
  prompt: string;
  images?: ImagePayload[];
  files?: FilePayload[];
  capabilityContexts?: CapabilityContext[];
  harnessProfileId?: string;
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

/** Project a queued create's session snapshot down to the exact wire shape.
 * Callers hand over full `Session` objects; extra fields (local-only flags
 * like `pinned`/`offline_pending`, stale raw provider/runner identity from
 * entries persisted before sessions became profile-based) would be rejected
 * by the backend batch validator, so they are stripped here — at the single
 * funnel every entry passes through on enqueue AND on read. */
function projectCreateSessionPayload(
  session: OfflineCreateSessionPayload,
): OfflineCreateSessionPayload {
  const record = session as Record<string, unknown>;
  const projected: Record<string, unknown> = {};
  for (const key of CREATE_SESSION_PAYLOAD_KEYS) {
    if (record[key] !== undefined) projected[key] = record[key];
  }
  return projected as unknown as OfflineCreateSessionPayload;
}

/** Normalize `create_session` entries to the durable wire contract:
 * - Project the session snapshot to `CREATE_SESSION_PAYLOAD_KEYS`.
 * - A non-canonical `session.id` (persisted by older code or minted where
 *   UUID generation was broken) is rejected by the backend as
 *   `client_session_id` (400), so the entry would 400-loop forever on every
 *   reconnect flush. Re-mint a canonical UUID, preserving the queued
 *   prompt/config — never drop the user's intent. */
export function normalizeQueueEntries(entries: OfflineQueueEntry[]): OfflineQueueEntry[] {
  return entries.map((entry) => {
    if (entry.type !== "create_session") return entry;
    const session = projectCreateSessionPayload(entry.session);
    if (!CANONICAL_UUID.test(session.id)) session.id = uuidv4();
    return { ...entry, session };
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
    scheduleMobileBackgroundSyncMirror();
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

  /** Take ownership of dispatching `clientId` for this tab. Read-modify-writes
   * against fresh storage so the claim is visible to every tab, and returns
   * false when another tab already owns the dispatch or the claim could not
   * be persisted — in both cases this tab must not put the action on the
   * wire. */
  const claimForDispatch = useCallback((clientId: string): boolean => {
    const base = readQueue();
    const target = base.find((entry) => entry.clientId === clientId);
    if (!target || target.dispatchedBy) return false;
    const next = base.map((entry) =>
      entry.clientId === clientId ? { ...entry, dispatchedBy: OFFLINE_TAB_ID } : entry
    );
    if (!writeQueueRaw(next)) return false;
    queueRef.current = next;
    setQueue(next);
    return true;
  }, []);

  /** Strip the claim from every entry the predicate selects. Writes nothing
   * when no claim matches: `commit` always mints a new array, and an
   * unconditional write here would re-render the flush effect into an endless
   * reap loop. */
  const dropClaims = useCallback(
    (matches: (entry: OfflineQueueEntry) => boolean) => {
      if (!readQueue().some((entry) => entry.dispatchedBy && matches(entry))) return true;
      return commit((prev) => prev.map((entry) => {
        if (!entry.dispatchedBy || !matches(entry)) return entry;
        const unclaimed = { ...entry };
        delete unclaimed.dispatchedBy;
        return unclaimed;
      }));
    },
    [commit],
  );

  const releaseDispatch = useCallback(
    (clientId: string) => dropClaims((entry) => entry.clientId === clientId),
    [dropClaims],
  );

  /** Drop this tab's dispatch claims. Called on connection loss: our socket
   * going away is the event that makes OUR in-flight actions undelivered.
   * Another tab's claims are none of our business — its socket may be fine. */
  const releaseOwnDispatched = useCallback(() => {
    return dropClaims((entry) => entry.dispatchedBy === OFFLINE_TAB_ID);
  }, [dropClaims]);

  /** Drop claims owned by tabs that no longer exist. A tab closed or crashed
   * between dispatching and its ack would otherwise strand the action in the
   * backlog forever. `null` means liveness is unknowable here — leave every
   * claim alone rather than risk duplicating a live tab's send. */
  const releaseDeadClaims = useCallback(
    (liveTabs: Set<string> | null) => {
      if (!liveTabs) return true;
      return dropClaims((entry) =>
        entry.dispatchedBy !== OFFLINE_TAB_ID && !liveTabs.has(entry.dispatchedBy!)
      );
    },
    [dropClaims],
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

  useEffect(() => {
    if (!Capacitor.isNativePlatform()) return;
    let cancelled = false;
    const reconcile = async () => {
      try {
        const acknowledged = await getMobileBackgroundAcknowledgements();
        if (cancelled || acknowledged.length === 0) return;
        const keys = new Set(
          acknowledged.map((item) => `${item.sessionId}\u0000${item.clientId}`),
        );
        if (!commit((prev) => prev.filter((entry) => !keys.has(entryIdentity(entry))))) return;
        await clearMobileBackgroundAcknowledgements();
      } catch {
        // Runner acknowledgements remain durable for the next foreground.
      }
    };
    void reconcile();
    let removeListener: (() => Promise<void>) | undefined;
    void CapacitorApp.addListener("appStateChange", ({ isActive }) => {
      if (isActive) void reconcile();
    }).then((handle) => {
      removeListener = () => handle.remove();
    });
    return () => {
      cancelled = true;
      void removeListener?.();
    };
  }, [commit]);

  return {
    queue,
    enqueue,
    getAll,
    remove,
    removeBySessionAndClient,
    claimForDispatch,
    releaseDispatch,
    releaseOwnDispatched,
    releaseDeadClaims,
    persistFailed,
  };
}
