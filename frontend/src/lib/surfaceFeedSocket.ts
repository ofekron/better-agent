// Generic ref-counted, lazy-open/close-on-idle `/ws/v2/surface` feed
// connection — the ONE shared implementation of the pattern every
// feed-only plane needs: open a dedicated connection with an empty cursor
// set (`open([], focus)`, never a chat-surface subscribe) + `setFeeds([...])`,
// opened on the first subscriber and closed once the last one
// unsubscribes, plus a `submit()`-with-late-rejection-window helper for
// planes whose intents can fail asynchronously after a synchronous accept
// (D4's pattern). `lib/systemFeedRegistry.ts`, `lib/runSummaryRegistry.ts`,
// `hooks/useProviderSurfaceSocket.ts`, and `lib/sessionSurfaceRegistry.ts`
// all build on this factory — each owns only its own plane-specific state
// projection (cold-hydrate maps, per-scope listeners, ref-correlation) on
// top of the transport this module provides; a registry that never closes
// its connection (`runSummaryRegistry`/`sessionSurfaceRegistry`) simply
// holds one internal `subscribeFrames` subscription open forever instead
// of ever calling its own unsubscribe — no separate "never close" factory
// mode needed, `releaseSocketIfIdle` already no-ops while any listener,
// internal or external, remains registered.

import { SurfaceClient, type SurfaceSocket, type SurfaceSocketHandlers } from "../adapter/client";
import { CONTRACT_VERSION, type FeedName, type Focus, type SurfaceIntentWire, type TransportAckFrame } from "../adapter/wire";
import { uuidv4 } from "./uuid";

/** Bounded window to wait for a late, async `intent_rejected` after a
 * synchronous `intent_accepted` — same rationale/value as
 * `useProviderSurfaceSocket.ts`'s `LATE_REJECTION_WINDOW_MS` (D4): a
 * fire-and-forget intent's mutation typically resolves well inside this
 * window; no rejection within it -> the original accept stands
 * (submit-and-wait-with-timeout-soft-fail). */
const LATE_REJECTION_WINDOW_MS = 1500;

export interface FeedSocketRegistry<TFrame extends { type: string }> {
  /** Subscribe to every live frame on this plane's feed(s). Opens the
   * shared connection on the first subscriber, closes it once the last
   * one unsubscribes. */
  subscribeFrames(listener: (frame: TFrame) => void): () => void;
  /** Subscribe to this connection's open/close transitions — fires once
   * synchronously with the CURRENT state on subscribe. */
  subscribeConnection(listener: (open: boolean) => void): () => void;
  isOpen(): boolean;
  /** Submits an intent over the shared connection. Returns `null`
   * synchronously (no queuing) when the connection isn't OPEN — the
   * caller falls back to REST, same not-open contract as
   * `SurfaceSocket.submit`/`submitProviderIntent`. `cv`/`session_id` are
   * always stamped here; `intent_id` is stamped too UNLESS the caller
   * already supplied one (`sessionSurfaceRegistry.ts`'s ref-correlation
   * needs to know the id up front, before this resolves, to correlate a
   * later live upsert against it — every other caller omits it and gets a
   * fresh one). The caller supplies only the intent-kind-specific fields
   * (typed precisely at ITS OWN boundary via `DistributiveOmit`, same as
   * `submitProviderIntent`/`wire.ts`'s own `DistributiveOmit` note — this
   * factory's own signature stays loosely typed on purpose, since a plain
   * `Omit` over a union type here would re-introduce the exact bug that
   * note documents). */
  submitIntent(
    intent: { kind: string; intent_id?: string } & Record<string, unknown>,
  ): Promise<TransportAckFrame> | null;
}

/** `attachFrameHandler` wires this plane's own `SurfaceSocketHandlers`
 * field (e.g. `onSystemFrame`) onto the shared handlers object — kept as
 * an injected function (rather than a `keyof SurfaceSocketHandlers`
 * string) so each plane's distinct frame parameter type stays real,
 * checked TypeScript at its one call site instead of an internal cast. */
export function createFeedSocketRegistry<TFrame extends { type: string }>(
  feeds: FeedName[],
  attachFrameHandler: (
    handlers: SurfaceSocketHandlers,
    dispatch: (frame: TFrame) => void,
  ) => void,
  /** `focus` for this connection's one-time `open([], focus)` call —
   * defaults to `"warm"` (every non-cursor feed connection's own default:
   * it isn't tied to any single session being the user's current view).
   * `useProviderSurfaceSocket.ts` passes `"opened"` instead, its own
   * pre-existing choice preserved verbatim by this factory retrofit. */
  options: { focus?: Focus } = {},
): FeedSocketRegistry<TFrame> {
  const focus = options.focus ?? "warm";
  const client = new SurfaceClient();
  let socket: SurfaceSocket | null = null;
  let socketOpen = false;
  const frameListeners = new Set<(frame: TFrame) => void>();
  const connectionListeners = new Set<(open: boolean) => void>();
  const lateRejectionListeners = new Set<(ack: TransportAckFrame) => void>();

  function ensureSocket(): void {
    if (socket) return;
    const handlers: SurfaceSocketHandlers = {
      onFrame: () => {
        // This connection never subscribes to `{"surfaces": [...]}` (chat
        // cursors) — only `{"feeds": [...]}` — so a `ChatFrame` should
        // never arrive here; ignored defensively rather than assumed.
      },
      onResyncRequired: () => {
        // No chat-surface cursor is ever subscribed on this connection.
      },
      onOpen: () => {
        socketOpen = true;
        for (const listener of connectionListeners) listener(true);
      },
      onClose: () => {
        socketOpen = false;
        for (const listener of connectionListeners) listener(false);
      },
      // Surface-agnostic late-rejection delivery (see `client.ts`'s
      // `onLateIntentRejected` docstring) — the ONE mechanism every
      // `createFeedSocketRegistry` plane relies on for
      // `waitForLateRejection` below, instead of each plane needing its
      // own frame union to carry `IntentRejectedFrame` as a member.
      onLateIntentRejected: (ack) => {
        for (const listener of lateRejectionListeners) listener(ack);
      },
    };
    attachFrameHandler(handlers, (frame) => {
      for (const listener of frameListeners) listener(frame);
    });
    socket = client.openSocket(handlers);
    socket.open([], focus);
    socket.setFeeds(feeds);
  }

  function releaseSocketIfIdle(): void {
    if (frameListeners.size > 0 || connectionListeners.size > 0) return;
    if (!socket) return;
    socket.close();
    socket = null;
    socketOpen = false;
  }

  function subscribeFrames(listener: (frame: TFrame) => void): () => void {
    ensureSocket();
    frameListeners.add(listener);
    return () => {
      frameListeners.delete(listener);
      releaseSocketIfIdle();
    };
  }

  function subscribeConnection(listener: (open: boolean) => void): () => void {
    ensureSocket();
    connectionListeners.add(listener);
    listener(socketOpen);
    return () => {
      connectionListeners.delete(listener);
      releaseSocketIfIdle();
    };
  }

  function isOpen(): boolean {
    return socketOpen;
  }

  function waitForLateRejection(
    intentId: string,
    acceptedAck: TransportAckFrame,
  ): Promise<TransportAckFrame> {
    return new Promise((resolve) => {
      let settled = false;
      const unsubscribe = ((): (() => void) => {
        const listener = (ack: TransportAckFrame) => {
          if (settled || ack.intent_id !== intentId) return;
          settled = true;
          clearTimeout(timer);
          off();
          resolve(ack);
        };
        lateRejectionListeners.add(listener);
        const off = () => lateRejectionListeners.delete(listener);
        return off;
      })();
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        unsubscribe();
        resolve(acceptedAck);
      }, LATE_REJECTION_WINDOW_MS);
    });
  }

  function submitIntent(
    intent: { kind: string; intent_id?: string } & Record<string, unknown>,
  ): Promise<TransportAckFrame> | null {
    if (!socket || !socketOpen) return null;
    const intentId = intent.intent_id ?? uuidv4();
    const submitted = socket.submit({
      // `session_id` stays whatever the caller supplied (the session
      // plane's own intents carry a real one, e.g. `mark_opened`'s target
      // session) — only defaulted to `null` when absent, the provider/
      // system planes' own convention (their `DistributiveOmit` excludes
      // `session_id` entirely, so it's always absent here for them).
      session_id: null,
      ...intent,
      cv: CONTRACT_VERSION,
      intent_id: intentId,
    } as unknown as SurfaceIntentWire);
    if (!submitted) return null;
    return submitted.then((ack) => (ack.type === "intent_rejected" ? ack : waitForLateRejection(intentId, ack)));
  }

  return { subscribeFrames, subscribeConnection, isOpen, submitIntent };
}
