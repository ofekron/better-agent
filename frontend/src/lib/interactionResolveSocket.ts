// Shared `/ws/v2/surface` connection for submitting `resolve` intents
// (ADR 0006 §5) — the WRITE side of the unified UserInteraction resource
// plane (tool/worker approval, delegation choice, user-input/memory
// proposal). Session-scoped at the INTENT level (every `ResolveInteraction`
// carries a real `session_id`), but the CONNECTION itself is not: unlike
// the per-session chat content socket (`surface/state.ts`'s `SurfaceStore`,
// opened only while that session's native rendering is active),
// `_handle_intent` (backend/adapter_api.py) dispatches purely by intent
// kind with no cursor-subscription requirement — so ONE shared, no-cursor
// connection (mirrors `hooks/useProviderSurfaceSocket.ts`'s pattern exactly,
// same reasoning) can resolve an interaction belonging to ANY session this
// connection's auth can reach, without tying resolution to whether that
// session's own v2 chat view happens to be open.
//
// READ side (pending-interaction hydration/listing) is explicitly NOT this
// module's concern — see hooks/usePendingUserInteractions.ts's docstring
// for why that hook stays on its legacy REST/eventBus path.
//
// Ref-counted like `useProviderSurfaceSocket.ts`: opened by the first
// acquirer (a mounted card that might need to resolve), closed once the
// last one releases. A resolve attempted before the connection finishes
// its handshake (or while offline) sees `submitResolveInteraction` return
// `null` and falls back to the legacy REST endpoint — the same "not open
// yet -> REST" contract every other v2 write path in this app uses.

import { useEffect, useState } from "react";
import { SurfaceClient, type SurfaceSocket } from "../adapter/client";
import {
  CONTRACT_VERSION,
  type InteractionRef,
  type ResolveResponseWire,
  type TransportAckFrame,
  type UserInteractionKindWire,
} from "../adapter/wire";
import { uuidv4 } from "./uuid";

// Single source for the 4 `interaction_ref` namespace prefixes (mirrors
// backend/surface_contract/nodes.py's `*_REF_PREFIX` constants) that any
// frontend caller needs to build a ref to resolve. Only the 2 prefixes
// with a frontend resolver call site so far are exported here — add the
// other 2 (`tool_approval:`/`worker_approval:`) if/when a caller besides
// Chat.tsx's inline hardcoded literals needs them.
export const USER_INPUT_REF_PREFIX = "user_input:";
export const DELEGATE_CHOICE_REF_PREFIX = "delegate_choice:";

const client = new SurfaceClient();
let socket: SurfaceSocket | null = null;
let socketOpen = false;
let refCount = 0;
const connectionListeners = new Set<(open: boolean) => void>();

function ensureSocket(): void {
  if (socket) return;
  socket = client.openSocket({
    onFrame: () => {
      // No `{"surfaces": [...]}` cursor is ever subscribed on this
      // connection — it exists purely to submit `resolve` intents.
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
  });
  socket.open([], "opened");
}

function releaseSocketIfIdle(): void {
  if (refCount > 0) return;
  if (!socket) return;
  socket.close();
  socket = null;
  socketOpen = false;
}

/** Acquire the shared connection for the caller's lifetime; call the
 * returned function to release. Multiple concurrent acquirers (e.g. two
 * pending cards mounted at once) share the SAME connection — closed only
 * once every acquirer has released. */
export function acquireInteractionResolveSocket(): () => void {
  ensureSocket();
  refCount += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    refCount -= 1;
    releaseSocketIfIdle();
  };
}

export function isInteractionResolveSocketOpen(): boolean {
  return socketOpen;
}

/** Submits a `resolve` intent (ADR 0006 §5) for one UserInteraction.
 * Returns `null` synchronously — same not-open contract as
 * `SurfaceSocket.submit`/`submitProviderIntent` — when the shared
 * connection isn't OPEN (no acquirer yet, mid-reconnect, or offline); the
 * caller falls back to the legacy REST resolve/cancel/decide endpoint for
 * that mechanism. Resolved value reflects the backend's synchronous
 * accept/reject ack (ADR 0006 §5: acks are transport-level only — the
 * caller's own optimistic UI update, same as every existing card, is what
 * actually clears the pending state; the real projection fact
 * (`user_interaction_upsert`, `state: resolved`) is not waited on here). */
export function submitResolveInteraction(
  sessionId: string,
  interactionRef: InteractionRef,
  interactionKind: UserInteractionKindWire,
  response: ResolveResponseWire,
): Promise<TransportAckFrame> | null {
  if (!socket || !socketOpen) return null;
  return socket.submit({
    kind: "resolve",
    cv: CONTRACT_VERSION,
    intent_id: uuidv4(),
    session_id: sessionId,
    interaction_ref: interactionRef,
    interaction_kind: interactionKind,
    response,
  });
}

/** Shared "native intent, else caller falls back to legacy REST" gate —
 * the exact idiom `UserInteractionCards.tsx`'s `resolveUserInput` and
 * `Chat.tsx`'s `ToolApprovalCard.decide` each inline separately: submit
 * via `submitResolveInteraction`, await whatever ack comes back (accepted
 * OR rejected both count as "handled" — a genuine `intent_rejected` is a
 * real failure, not a signal to retry over REST, since both paths reach
 * the SAME store mutation and would fail identically), and report `false`
 * only when the connection wasn't open at all so the caller knows to run
 * its own REST fallback. Callers whose kind (`choice`) has no other
 * inlined copy of this gate use this directly instead of forking a 3rd.
 *
 * NOT unified with `lib/nativeOrRestMutation.ts` or `useSession.ts`'s
 * `trySessionIntentV2` despite the superficially similar shape — see
 * `nativeOrRestMutation.ts`'s own docstring for why a rejection means
 * something different in each of the three. */
export async function tryResolveInteraction(
  sessionId: string,
  interactionRef: InteractionRef,
  interactionKind: UserInteractionKindWire,
  response: ResolveResponseWire,
): Promise<boolean> {
  const ack = await submitResolveInteraction(sessionId, interactionRef, interactionKind, response);
  return ack !== null;
}

/** React convenience wrapper: acquires the shared connection for the
 * component's mounted lifetime and reports its live open/closed state —
 * for a card that wants to gate its own UI (or just ensure the connection
 * is warm before the user clicks resolve). The lazy initializer reads the
 * CURRENT state at mount (correct even when a prior consumer already has
 * the connection open); every subsequent transition arrives via the
 * subscribed listener, never a synchronous call from the effect body. */
export function useInteractionResolveSocket(): { open: boolean } {
  const [open, setOpen] = useState(isInteractionResolveSocketOpen);
  useEffect(() => {
    const release = acquireInteractionResolveSocket();
    connectionListeners.add(setOpen);
    return () => {
      connectionListeners.delete(setOpen);
      release();
    };
  }, []);
  return { open };
}
