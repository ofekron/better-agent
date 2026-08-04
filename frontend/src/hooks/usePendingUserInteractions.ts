import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { eventBus } from "../lib/eventBus";
import type { UserInteractionRequest } from "../types";
import { notifyUserRequest } from "../utils/userInputNotifications";

// Every kind `user_input_store._public` can emit. `memory` MUST be here:
// the store returns memory proposals from /api/user-input/pending and
// Chat renders them via MemoryProposalCard, so dropping the kind here
// would make a snapshot silently delete a card the WS path just added.
const RENDERABLE_KINDS = new Set(["input", "approval", "memory"]);

const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 30000;

function pendingRequests(value: unknown): UserInteractionRequest[] {
  if (!Array.isArray(value)) return [];
  return value.filter((request): request is UserInteractionRequest => {
    if (!request || typeof request !== "object") return false;
    const candidate = request as Partial<UserInteractionRequest>;
    return (
      candidate.status === "pending" &&
      typeof candidate.request_id === "string" &&
      typeof candidate.app_session_id === "string" &&
      typeof candidate.kind === "string" &&
      RENDERABLE_KINDS.has(candidate.kind)
    );
  });
}

async function loadPendingRequests(): Promise<UserInteractionRequest[] | null> {
  try {
    const response = await fetch(`${API}/api/user-input/pending`, { credentials: "include" });
    if (!response.ok) return null;
    const data = await response.json();
    return pendingRequests(data.requests);
  } catch {
    return null;
  }
}

function byCreatedAt(a: UserInteractionRequest, b: UserInteractionRequest): number {
  const at = Number(a.created_at ?? 0);
  const bt = Number(b.created_at ?? 0);
  if (at !== bt) return at - bt;
  return a.request_id.localeCompare(b.request_id);
}

/** Pending request_user_input / approval / memory-proposal cards.
 *
 * The backend store is the only source of truth; this hook mirrors it
 * from `GET /api/user-input/pending` and patches from WS events. Two
 * properties it MUST keep:
 *
 * 1. It re-pulls the snapshot whenever the WebSocket (re)connects and
 *    whenever the tab becomes visible again. `user_input_requested` is
 *    session-scoped (`coordinator.dispatch_raw`) and
 *    `session_user_input_changed` is fire-and-forget
 *    (`coordinator.broadcast_global`); neither is replayed on
 *    reconnect. Without the re-pull, any request created while this
 *    client was disconnected — a backgrounded mobile tab, a network
 *    switch, a sleeping laptop — stays invisible until a page reload.
 * 2. A snapshot MERGES, never replaces. A rehydrate racing an
 *    incoming request must not drop the live card, and a rehydrate
 *    racing a local resolve must not resurrect the resolved one. Live
 *    WS mutations stamp a monotonic token per request id; a snapshot
 *    only overrides ids whose last local mutation predates the fetch.
 */
export function usePendingUserInteractions() {
  const { t } = useTranslation();
  const [requests, setRequests] = useState<UserInteractionRequest[]>([]);
  // Working copy every mutation reads and writes. State updaters run
  // asynchronously, so the merge cannot compute (or read its result)
  // inside one — the notify/known-id bookkeeping needs the next list
  // synchronously.
  const requestsRef = useRef<UserInteractionRequest[]>([]);
  const knownIdsRef = useRef<Set<string>>(new Set());
  // Monotonic clock for live WS mutations + the last tick per request id.
  const tickRef = useRef(0);
  const localTicksRef = useRef<Map<string, number>>(new Map());
  // Only the newest in-flight fetch may apply its result, so a slow
  // earlier snapshot can't clobber a newer one. WS events never
  // invalidate a fetch — that is what the merge is for.
  const fetchIdRef = useRef(0);
  const retryAttemptRef = useRef(0);
  const inFlightRef = useRef<Promise<void> | null>(null);
  const queuedRef = useRef(false);
  const queuedNotifyRef = useRef(false);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const mountedRef = useRef(true);

  const commit = useCallback((next: UserInteractionRequest[]) => {
    requestsRef.current = next;
    setRequests(next);
  }, []);

  const notify = useCallback((request: UserInteractionRequest) => {
    void notifyUserRequest(request, t("userApproval.title"), t("userInput.title"), {
      add: t("memoryProposal.titleAdd"),
      edit: t("memoryProposal.titleEdit"),
    });
  }, [t]);

  const clearRetry = useCallback(() => {
    if (retryTimerRef.current !== undefined) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = undefined;
    }
  }, []);

  const refetch = useCallback(async (notifyNew = false): Promise<boolean> => {
    const fetchId = ++fetchIdRef.current;
    const startedAtTick = tickRef.current;
    const snapshot = await loadPendingRequests();
    if (!mountedRef.current || fetchId !== fetchIdRef.current) return snapshot !== null;
    if (snapshot === null) return false;

    const snapshotIds = new Set(snapshot.map((request) => request.request_id));
    const merged = new Map<string, UserInteractionRequest>();
    for (const request of requestsRef.current) merged.set(request.request_id, request);
    // Drop what the snapshot says is gone — unless a live event touched
    // it after this fetch started (it is newer than us).
    for (const id of [...merged.keys()]) {
      if (snapshotIds.has(id)) continue;
      if ((localTicksRef.current.get(id) ?? 0) > startedAtTick) continue;
      merged.delete(id);
    }
    // Adopt the snapshot's rows, skipping ids a live event touched after
    // this fetch started: such an id was either added locally (already
    // present) or resolved locally (must stay absent).
    for (const request of snapshot) {
      if ((localTicksRef.current.get(request.request_id) ?? 0) > startedAtTick) continue;
      merged.set(request.request_id, request);
    }
    commit([...merged.values()].sort(byCreatedAt));
    // Forget ticks for ids neither side knows about — otherwise the map
    // grows for the tab's whole lifetime.
    for (const id of [...localTicksRef.current.keys()]) {
      if (merged.has(id) || snapshotIds.has(id)) continue;
      localTicksRef.current.delete(id);
    }

    if (notifyNew) {
      for (const request of merged.values()) {
        if (!knownIdsRef.current.has(request.request_id)) notify(request);
      }
    }
    knownIdsRef.current = new Set(merged.keys());
    return true;
  }, [commit, notify]);

  // A failed pull is retried with bounded backoff: on a cold load the
  // fetch can lose the race with the auth/bootstrap gate, and there is
  // no later event guaranteed to arrive and re-trigger it.
  const hydrateRef = useRef<(notifyNew: boolean) => Promise<void>>(async () => {});
  const hydrate = useCallback((notifyNew: boolean): Promise<void> => {
    // Single-flight. Reconnect and visibilitychange co-fire on mobile
    // resume — the exact path this hook exists to cover — so a second
    // trigger must not open a second GET. It is queued instead of
    // dropped: the in-flight snapshot may predate whatever prompted it.
    if (inFlightRef.current) {
      queuedRef.current = true;
      queuedNotifyRef.current = queuedNotifyRef.current || notifyNew;
      return inFlightRef.current;
    }
    const run = (async () => {
      let notifyThisRound = notifyNew;
      for (;;) {
        queuedRef.current = false;
        clearRetry();
        const ok = await refetch(notifyThisRound);
        if (ok || !mountedRef.current) {
          retryAttemptRef.current = 0;
        } else {
          const delay = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** retryAttemptRef.current);
          retryAttemptRef.current += 1;
          retryTimerRef.current = setTimeout(() => { void hydrateRef.current(notifyThisRound); }, delay);
        }
        if (!queuedRef.current || !mountedRef.current) return;
        notifyThisRound = queuedNotifyRef.current;
        queuedNotifyRef.current = false;
      }
    })();
    inFlightRef.current = run;
    void run.finally(() => { inFlightRef.current = null; });
    return run;
  }, [clearRetry, refetch]);
  useEffect(() => {
    hydrateRef.current = hydrate;
  }, [hydrate]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      clearRetry();
    };
  }, [clearRetry]);

  // One-shot: goes through the ref so a new `t` identity (language load
  // or switch) can't re-trigger the initial pull.
  useEffect(() => {
    void hydrateRef.current(false);
  }, []);

  useEffect(() => {
    const touch = (requestId: string) => {
      tickRef.current += 1;
      localTicksRef.current.set(requestId, tickRef.current);
    };
    const onRequested = (request: UserInteractionRequest) => {
      if (!request || request.status !== "pending") return;
      touch(request.request_id);
      const isNew = !knownIdsRef.current.has(request.request_id);
      knownIdsRef.current.add(request.request_id);
      commit([
        ...requestsRef.current.filter((item) => item.request_id !== request.request_id),
        request,
      ].sort(byCreatedAt));
      if (isNew) notify(request);
    };
    const onResolved = (detail: { request_id: string }) => {
      if (!detail?.request_id) return;
      touch(detail.request_id);
      knownIdsRef.current.delete(detail.request_id);
      commit(requestsRef.current.filter((item) => item.request_id !== detail.request_id));
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") void hydrateRef.current(true);
    };
    const offChanged = eventBus.subscribe("session_user_input_changed", () => {
      void hydrateRef.current(true);
    });
    // Reconnect rehydrate: everything broadcast during the disconnect
    // window was dropped, so the snapshot is the only way back.
    const offConnection = eventBus.subscribe("ws_connection_changed", ({ connected }) => {
      if (connected) void hydrateRef.current(true);
    });
    const offRequested = eventBus.subscribe("user_input_requested", onRequested);
    const offResolved = eventBus.subscribe("user_input_resolved", onResolved);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      offChanged();
      offConnection();
      offRequested();
      offResolved();
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [commit, notify]);

  const removeRequest = useCallback((requestId: string) => {
    tickRef.current += 1;
    localTicksRef.current.set(requestId, tickRef.current);
    knownIdsRef.current.delete(requestId);
    commit(requestsRef.current.filter((request) => request.request_id !== requestId));
  }, [commit]);

  return { requests, removeRequest };
}
