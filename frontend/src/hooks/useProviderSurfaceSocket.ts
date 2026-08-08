// Package D (ADR 0007): the provider/auth plane's single shared
// `/ws/v2/surface` connection — one socket for every provider-plane hook
// and component (useProviderChanged, useModelsCatalogChanged,
// SettingsPage, ProviderForm, RuntimeProfileWizard), never one per
// consumer. Reuses `SurfaceClient`/`SurfaceSocket` verbatim (the same
// transport class `surface/state.ts`'s per-session `SurfaceStore` uses for
// the chat plane) — this module is the provider plane's counterpart to
// that store, minus any node/turn state (ADR 0007 has none).
//
// Opened lazily on first subscriber, closed when the last one unmounts —
// mirrors `lib/runSummaryRegistry.ts`'s pattern for the "runs" feed (a
// process-wide, ref-counted feed subscription is the established idiom
// here, not novel to this module).
//
// Deliberately NOT gated behind the `ba.surface_v2` kill-switch
// (adapter/flag.ts): that flag governs whether Chat.tsx renders via the
// new node-based surface vs. the legacy message pipeline (its own
// docstring: "Chat Surface Contract v2 thin client") — a chat-rendering
// concern, not "should any v2 endpoint be used at all". `lib/
// runSummaryRegistry.ts`'s sibling "runs" feed connection is the
// established precedent for this same reasoning — it does not check the
// flag either. Every consumer of this module additionally keeps its
// legacy REST/eventBus path active regardless (see useProviderChanged.ts/
// useModelsCatalogChanged.ts's dual-subscribe docstrings), so there is no
// user-visible regression if this connection is ever unreachable.

import { createFeedSocketRegistry } from "../lib/surfaceFeedSocket";
import type { DistributiveOmit, ProviderFrame, ProviderIntentWire, TransportAckFrame } from "../adapter/wire";

type ProviderFrameListener = (frame: ProviderFrame) => void;
type ConnectionListener = (open: boolean) => void;

/** Transport for the `providers` feed, built on the shared ref-counted
 * factory (`lib/surfaceFeedSocket.ts`) — `focus: "opened"` (not the
 * factory's own `"warm"` default) is this plane's own pre-existing choice,
 * preserved verbatim by this retrofit. */
const registry = createFeedSocketRegistry<ProviderFrame>(
  ["providers"],
  (handlers, dispatch) => {
    handlers.onProviderFrame = dispatch;
  },
  { focus: "opened" },
);

/** Subscribe to every live `"providers"` feed frame
 * (`provider_upsert`/`credential_state`/`model_catalog_changed`/
 * `login_flow_frame`). Opens the shared connection on the first
 * subscriber, closes it once the last one unsubscribes. */
export function subscribeProviderFrames(listener: ProviderFrameListener): () => void {
  return registry.subscribeFrames(listener);
}

/** Subscribe to this connection's open/close transitions (e.g. to gate
 * "submit via intent vs. fall back to REST" eligibility). Fires once
 * synchronously with the CURRENT state on subscribe, like `onOpen`/
 * `onClose` would have already told a listener that mounted after connect. */
export function subscribeProviderSocketConnection(listener: ConnectionListener): () => void {
  return registry.subscribeConnection(listener);
}

export function isProviderSocketOpen(): boolean {
  return registry.isOpen();
}

/** Submits a `ProviderIntent` (ADR 0007) over the shared connection.
 * Returns `null` synchronously — exactly `SurfaceSocket.submit`'s own
 * not-open contract — when the connection isn't OPEN (or the flag is
 * off / no subscriber has opened it yet); the caller falls back to REST.
 * `cv`/`intent_id`/`session_id` are stamped here so every call site only
 * supplies the intent-specific fields.
 *
 * The resolved promise reflects the FINAL outcome, not just admission: a
 * synchronous `intent_rejected` (malformed payload, loopback gate)
 * resolves immediately; a synchronous `intent_accepted` still waits up to
 * the factory's own late-rejection window for a late async
 * `intent_rejected` echo before the caller can trust it — so every call
 * site can treat this exactly like the REST call it replaces: `await`,
 * then branch on `.type`. */
export function submitProviderIntent(
  intent: DistributiveOmit<ProviderIntentWire, "cv" | "intent_id" | "session_id">,
): Promise<TransportAckFrame> | null {
  return registry.submitIntent(intent as { kind: string } & Record<string, unknown>);
}
