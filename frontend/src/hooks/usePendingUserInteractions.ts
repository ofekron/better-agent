import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { SurfaceClient } from "../adapter/client";
import type { UserInteractionWire } from "../adapter/wire";
import { API } from "../api";
import { eventBus } from "../lib/eventBus";
import { USER_INPUT_REF_PREFIX } from "../lib/interactionResolveSocket";
import type { MemoryProposal, UserInputQuestion, UserInteractionRequest } from "../types";
import { notifyUserRequest } from "../utils/userInputNotifications";

// Every kind `user_input_store._public` can emit. `memory` MUST be here:
// the store returns memory proposals from /api/user-input/pending and
// Chat renders them via MemoryProposalCard, so dropping the kind here
// would make a snapshot silently delete a card the WS path just added.
const RENDERABLE_KINDS = new Set(["input", "approval", "memory"]);

const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 30000;

const v2Client = new SurfaceClient();

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

/** ADR 0006 §5's v2 `UserInteraction` resource has no `created_at` field
 * (`request`/`state`/`response` only) — `fetchedAt` (the moment this hook
 * pulled the snapshot) is the best available ordering key for v2-sourced
 * rows. Pending-interaction cards are low-frequency, human-latency events;
 * approximate ordering here is a documented trade-off, not a correctness
 * gap (the SAME set of pending rows renders either way, only their
 * relative order among themselves in one merged list is approximate). */
function mapWireInteractionToRequest(
  sessionId: string,
  wire: UserInteractionWire,
  fetchedAt: number,
): UserInteractionRequest | null {
  // Only the `input` kind (free-form questions / approval-prompt / memory
  // proposal — all 3 legacy `user_input_store` sub-kinds collapse onto
  // this one UserInteractionKind, ADR 0006 §5's kind-mapping table) renders
  // through this hook's card set. `approval` (tool/worker-creation) and
  // `choice` (delegation picker) are separate mechanisms with their own
  // hydration elsewhere (Chat.tsx's pendingToolApprovals/pendingApprovals,
  // App.tsx's resolveDelegation) — filtered out rather than mis-rendered.
  if (wire.kind !== "input") return null;
  const requestId = wire.interaction_ref.startsWith(USER_INPUT_REF_PREFIX)
    ? wire.interaction_ref.slice(USER_INPUT_REF_PREFIX.length)
    : wire.interaction_ref;
  const status: UserInteractionRequest["status"] =
    wire.state === "pending" ? "pending" : wire.state === "cancelled" ? "cancelled" : "resolved";
  const schema = (wire.request?.schema ?? {}) as {
    kind?: string;
    questions?: UserInputQuestion[];
    memory_proposal?: MemoryProposal;
  };
  const base = { request_id: requestId, app_session_id: sessionId, status, created_at: fetchedAt };
  if (schema.kind === "approval") {
    return { ...base, kind: "approval", prompt: String(wire.request?.prompt ?? "") };
  }
  if (schema.kind === "memory" && schema.memory_proposal) {
    return { ...base, kind: "memory", memory_proposal: schema.memory_proposal };
  }
  return { ...base, kind: "input", questions: schema.questions ?? [] };
}

/** Pending request_user_input / approval / memory-proposal cards.
 *
 * Two source planes feed this hook's merged `requests`, split per the
 * surface-migration Package A design ruling:
 *
 * 1. `currentSessionId` (the session the caller currently has open, or
 *    `null`) hydrates its `input`-kind pending interactions from the v2
 *    `UserInteraction` resource plane (ADR 0006 §5): `GET /api/v2/surface/
 *    sessions/:id/snapshot`'s `interactions` field on load/session-change,
 *    re-pulled whenever a legacy `user_input_requested`/`_resolved`/
 *    `session_user_input_changed` event, a WS reconnect, or tab visibility
 *    signals something may have changed. These legacy events remain fully
 *    live on the wire (the v2 migration only ADDED `interaction.fire.*`
 *    facts, per Package A backend outcomes — it never removed the
 *    pre-existing broadcasts), so they are trustworthy free triggers; only
 *    the DATA read in response to them is v2. `user_interaction_upsert`
 *    (the live WS frame counterpart) is NOT separately subscribed here —
 *    doing so needs a second per-session cursor connection alongside
 *    Chat.tsx's own `surface/state.ts` `SurfaceStore` connection for that
 *    same session (which already receives this exact frame type on the
 *    wire today and currently drops it, see that file's `default:` case
 *    comment) with no established "second cursor consumer" pattern
 *    anywhere else in this codebase (every other cross-cutting v2 consumer
 *    uses a non-cursor `{"feeds":[...]}` push instead) — flagged as the
 *    genuine remaining gap for whoever wires that plumbing, not silently
 *    declared solved.
 * 2. Every OTHER session's pending interactions (feeds `backgroundUser
 *    Interactions`, the cross-session toast) stay on the fully legacy
 *    `GET /api/user-input/pending` + eventBus path below, unchanged —
 *    ADR 0006 §0 scopes every v2 resource to ONE session and no v2
 *    endpoint lists pending interactions across every session a user can
 *    see (traced during the prior Package A frontend pass; still true).
 *
 * The legacy hydration/merge machinery below (properties 1/2 in its own
 * original docstring) is otherwise UNCHANGED — it remains the source of
 * truth for `currentSessionId === null` and for every non-current session,
 * and is what `currentSessionId`'s v2 rows are unioned against.
 */
export function usePendingUserInteractions(currentSessionId: string | null = null) {
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

  // v2 current-session plane (see docstring point 1). Tagged with the
  // session id it was hydrated for so a session switch can be detected
  // (and legacy fallback used) purely by comparing `sessionId` in the
  // merge below — no separate "reset to null on session change" effect
  // needed, which would otherwise call `setState` synchronously in an
  // effect body.
  const [v2State, setV2State] = useState<{ sessionId: string; requests: UserInteractionRequest[] } | null>(null);
  const v2SessionRef = useRef<string | null>(null);
  const v2FetchIdRef = useRef(0);

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

  // --- v2 current-session hydration (docstring point 1) ---------------

  const hydrateV2 = useCallback(async (sessionId: string): Promise<void> => {
    const fetchId = ++v2FetchIdRef.current;
    const envelope = await v2Client.fetchSnapshot(sessionId).catch(() => null);
    if (!mountedRef.current || fetchId !== v2FetchIdRef.current) return;
    if (v2SessionRef.current !== sessionId) return; // session changed mid-flight
    if (!envelope || envelope.kind !== "ok") return; // rebuilding/stale_cursor/network error: keep the prior value, a later trigger retries
    const fetchedAt = Date.now();
    const mapped: UserInteractionRequest[] = [];
    for (const wire of envelope.interactions) {
      const mappedRequest = mapWireInteractionToRequest(sessionId, wire, fetchedAt);
      if (mappedRequest && mappedRequest.status === "pending") mapped.push(mappedRequest);
    }
    setV2State({ sessionId, requests: mapped });
  }, []);
  // Routed through a ref (same pattern as `hydrateRef` above) so the
  // effects below call `hydrateV2Ref.current(...)` rather than the raw
  // callback — keeps the eventual `setV2State` out of react-hooks/
  // set-state-in-effect's static reach from an effect body, same as the
  // pre-existing `hydrateRef` indirection this mirrors.
  const hydrateV2Ref = useRef(hydrateV2);
  useEffect(() => {
    hydrateV2Ref.current = hydrateV2;
  }, [hydrateV2]);

  useEffect(() => {
    v2SessionRef.current = currentSessionId;
    if (!currentSessionId) return;
    void hydrateV2Ref.current(currentSessionId);
  }, [currentSessionId]);

  useEffect(() => {
    if (!currentSessionId) return;
    const rehydrate = () => void hydrateV2Ref.current(currentSessionId);
    const offChanged = eventBus.subscribe("session_user_input_changed", rehydrate);
    const offConnection = eventBus.subscribe("ws_connection_changed", ({ connected }) => {
      if (connected) rehydrate();
    });
    const offRequested = eventBus.subscribe("user_input_requested", (request: UserInteractionRequest) => {
      if (request?.app_session_id === currentSessionId) rehydrate();
    });
    // The legacy `user_input_resolved` payload carries no `app_session_id`
    // — rehydrating unconditionally is a single cheap REST GET, triggered
    // only by a genuine human resolve/cancel event (not a hot path).
    const offResolved = eventBus.subscribe("user_input_resolved", rehydrate);
    const onVisible = () => {
      if (document.visibilityState === "visible") rehydrate();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      offChanged();
      offConnection();
      offRequested();
      offResolved();
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [currentSessionId]);

  const removeRequest = useCallback((requestId: string) => {
    tickRef.current += 1;
    localTicksRef.current.set(requestId, tickRef.current);
    knownIdsRef.current.delete(requestId);
    commit(requestsRef.current.filter((request) => request.request_id !== requestId));
    setV2State((prev) =>
      prev ? { ...prev, requests: prev.requests.filter((request) => request.request_id !== requestId) } : prev,
    );
  }, [commit]);

  // Final merge: `currentSessionId`'s rows come from v2 once hydrated for
  // THAT session (`v2State?.sessionId === currentSessionId` — false right
  // after a session switch, until the new session's v2 pull lands, so the
  // UI never flashes empty); every other session's rows (and
  // `currentSessionId`'s rows before its first v2 pull lands) come from
  // the legacy list above, unchanged.
  const merged = useMemo(() => {
    if (!currentSessionId || v2State?.sessionId !== currentSessionId) return requests;
    const others = requests.filter((request) => request.app_session_id !== currentSessionId);
    return [...others, ...v2State.requests].sort(byCreatedAt);
  }, [requests, v2State, currentSessionId]);

  return { requests: merged, removeRequest };
}
